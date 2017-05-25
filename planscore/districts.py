import collections, json, io, gzip
from osgeo import ogr
import boto3, botocore.exceptions
from . import prepare_state, score

ogr.UseExceptions()

FUNCTION_NAME = 'PlanScore-RunDistrict'

class Storage:
    ''' Wrapper for S3-related details.
    '''
    def __init__(self, s3, bucket, prefix):
        self.s3 = s3
        self.bucket = bucket
        self.prefix = prefix
    
    @staticmethod
    def from_event(event, s3):
        bucket = event.get('bucket')
        prefix = event.get('prefix')
        return Storage(s3, bucket, prefix)

class Partial:
    ''' Partially-calculated district sums, used by consume_tiles().
    '''
    def __init__(self, totals, precincts, tiles, geometry):
        self.totals = totals
        self.precincts = precincts
        self.tiles = tiles
        self.geometry = geometry
    
    def to_dict(self):
        return dict(totals=self.totals, precincts=self.precincts, tiles=self.tiles)
    
    @staticmethod
    def from_event(event):
        totals = event.get('totals')
        precincts = event.get('precincts')
        tiles = event.get('tiles')
        geometry = ogr.CreateGeometryFromWkt(event['geometry'])
    
        if totals is None or precincts is None or tiles is None:
            totals, precincts, tiles = collections.defaultdict(int), [], get_geometry_tile_zxys(geometry)
        
        return Partial(totals, precincts, tiles, geometry)

def lambda_handler(event, context):
    '''
    '''
    partial = Partial.from_event(event)
    storage = Storage.from_event(event, boto3.client('s3'))
    
    for _ in consume_tiles(storage, partial):
        print('Iteration:', json.dumps(partial.to_dict()))

def consume_tiles(storage, partial):
    ''' Generate a stream of steps, updating totals from precincts and tiles.
    
        Inputs are modified directly, and lists should be empty at completion.
    '''
    for precinct in iterate_precincts(storage, partial.precincts, partial.tiles):
        score_precinct(partial, precinct)
        yield

def score_precinct(partial, precinct):
    '''
    '''
    precinct_geom = ogr.CreateGeometryFromJson(json.dumps(precinct['geometry']))
    try:
        overlap_geom = precinct_geom.Intersection(partial.geometry)
    except RuntimeError as e:
        if 'TopologyException' in str(e) and not precinct_geom.IsValid():
            # Sometimes, a precinct geometry can be invalid
            # so inflate it by a tiny amount to smooth out problems
            precinct_geom = precinct_geom.Buffer(0.0000001)
        else:
            raise
    overlap_area = overlap_geom.Area() / precinct_geom.Area()
    precinct_fraction = overlap_area * precinct['properties'][prepare_state.FRACTION_FIELD]
    
    for name in score.FIELD_NAMES:
        precinct_value = precinct_fraction * precinct['properties'][name]
        partial.totals[name] += precinct_value

def load_tile_precincts(storage, tile_zxy):
    ''' Get GeoJSON features for a specific tile.
    '''
    try:
        object = storage.s3.get_object(Bucket=storage.bucket,
            Key='{}/{}.geojson'.format(storage.prefix, tile_zxy))
    except botocore.exceptions.ClientError as error:
        if error.response['Error']['Code'] == 'NoSuchKey':
            return []
        raise

    if object.get('ContentEncoding') == 'gzip':
        object['Body'] = io.BytesIO(gzip.decompress(object['Body'].read()))
    
    geojson = json.load(object['Body'])
    return geojson['features']

def iterate_precincts(storage, precincts, tiles):
    ''' Generate a stream of precincts, getting new ones from tiles as needed.
    
        Input lists are modified directly, and should be empty at completion.
    '''
    while precincts or tiles:
        if precincts:
            # There is a precinct to yield.
            precinct = precincts.pop(0)
            yield precinct
    
        if tiles and not precincts:
            # All out of precincts; fill up from the next tile.
            tile_zxy = tiles.pop(0)
            more_precincts = load_tile_precincts(storage, tile_zxy)
            precincts.extend(more_precincts)

def get_geometry_tile_zxys(district_geom):
    ''' Return a list of expected tile Z/X/Y strings.
    '''
    if district_geom.GetSpatialReference():
        district_geom.TransformTo(prepare_state.EPSG4326)
    
    xxyy_extent = district_geom.GetEnvelope()
    iter = prepare_state.iter_extent_tiles(xxyy_extent, prepare_state.TILE_ZOOM)
    tiles = []

    for (coord, tile_wkt) in iter:
        tile_zxy = '{zoom}/{column}/{row}'.format(**coord.__dict__)
        tile_geom = ogr.CreateGeometryFromWkt(tile_wkt)
        
        if tile_geom.Intersects(district_geom):
            tiles.append(tile_zxy)
    
    return tiles