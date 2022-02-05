#!/usr/bin/python
# -*- coding: utf-8 -*-

import webapp2
import logging
import jinja2
import os
import json

from google.appengine.api import users
from google.appengine.api import namespace_manager
from google.appengine.api.users import create_logout_url

def auth(target):
	def authorized(self, *args, **kwargs):
		if not users.is_current_user_admin():
			self.redirect(users.create_login_url())
			logger.warn('Request denied for user %s', users.get_current_user())
			return
		return target(self, *args, **kwargs)

	return authorized


jinja_environment = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.dirname(__file__) + '/template/admin'))
jinja_environment.globals['logout'] = create_logout_url('http://wikinote.me')

class Main(webapp2.RequestHandler):

	@auth
	def get(self):
		from main import get_apps
		apps = get_apps()

		template = jinja_environment.get_template('index.html')
		self.response.out.write(template.render({'apps': apps}))


#####################
####    Stats    ####
#####################

from google.appengine.ext import db
from google.appengine.api import logservice

# model to store statistics
class Stat(db.Model):
	name = db.StringProperty(required=True)
	time = db.DateTimeProperty(auto_now_add=True)
	count = db.IntegerProperty(indexed=False)
	total = db.IntegerProperty(indexed=False)


STAT_COUNT = 0
STAT_TOTAL = 1
STAT_MARKER = 'stat '
logger = logging.getLogger('admin')

def log_stat(name):
	logger.info(STAT_MARKER + name)

def count_stat():
	import time
	start_time = int(time.time() - 300)
	logs = logservice.fetch(include_app_logs=True, start_time=start_time, batch_size=1000)

	data = {}
	n = 0
	for l in logs:
		n = n + 1
		for line in l.app_logs:
			if line.message.startswith(STAT_MARKER):
				name = line.message[len(STAT_MARKER): ]

				if name in data:
					s = data[name]
				else:
					s = (0, 0)
				data[name] = (s[STAT_COUNT] + 1, s[STAT_TOTAL] + l.latency)
				break

	if len(data) > 0:
		_save_stat(data)

def _save_stat(data):
	namespace_manager.set_namespace('admin')
	for name, values in data.iteritems():
		s = Stat(name=name, count=values[STAT_COUNT], total=int(values[STAT_TOTAL] * 1000))
		s.put()


class StatTask(webapp2.RequestHandler):

	def post(self):
		count_stat()

	def get(self):
		self.post()

class StatApi(webapp2.RequestHandler):
	def get(self, name):
		chart = [
          ['Time', 'Count', 'Total'],
		]    

		data = []
		stats = Stat.all().filter('name', name).order('-time').fetch(50)
		for s in stats:
			data.append( [ int(s.time.strftime('%s'))*1000, 10*int(s.count), int(s.total)/int(s.count)] )
		data.reverse()
		chart.extend(data)

		self.response.out.write(json.dumps(chart))



URL_MIGRATE = '/.task/migrate'

class TaskMigrate(webapp2.RequestHandler):

	def post(self):
		self.get()
		
	def get(self):
		app = self.request.get('app')
		if not app:
			from main import migrate_all
			migrate_all()
		else:
			namespace_manager.set_namespace(app)
			from page import migrate
			logger.info('Migrate app '+app)
			migrate()




admin_routes = [

	(r'/', Main),
	(r'/stat/task', StatTask),
	(r'/stat/api/<:.*>', StatApi),
	(URL_MIGRATE, TaskMigrate),
	
]