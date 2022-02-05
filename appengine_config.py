import os

def webapp_add_wsgi_middleware(app):
    from google.appengine.ext.appstats import recording
    app = recording.appstats_wsgi_middleware(app)
    return app

def namespace_manager_default_namespace_for_request():
	name = os.environ['SERVER_NAME']
	return name.split('.')[0]
