"""Test-only socket-free bridge to committed FeatureAPI and real persistent services."""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from opendevengine.platform import Platform
from opendevengine.services import ApplicationServices
from opendevengine.platform.errors import PlatformError
from opendevengine.api import error_status
request = json.load(sys.stdin)
platform = Platform.open(Path(sys.argv[1]))
try:
    services = ApplicationServices(platform)
    response = services.api.dispatch(request['method'], request['path'], request['query'], request.get('body'))
    print(json.dumps({'status': response.status, 'body': response.body} if response else {'status': 404, 'body': {'code': 'not_found'}}))
except PlatformError as error:
    print(json.dumps({'status': error_status(error.code), 'body': {'code': error.code}}))
finally:
    platform.close()
