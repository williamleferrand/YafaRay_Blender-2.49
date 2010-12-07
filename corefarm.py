from __future__ import with_statement

import logging
import os.path
import re
import socket
import tarfile
import time
import urllib
import urllib2
import Blender
import simplejson

from hashlib import sha256
import MultipartPostHandler


DEBUG_HTTP = True
COREFARM_API = 'http://gateway.corefarm.com/'
S3_HOST = 'http://corefarm-data.s3.amazonaws.com/'
USER_AGENT = 'Blender-Yafaray-Exporter/1.0'

if DEBUG_HTTP:
	import httplib
	httplib.HTTPConnection.debuglevel = 1

	opener = urllib2.build_opener(
		urllib2.HTTPHandler(debuglevel = True),
		MultipartPostHandler.MultipartPostHandler,
	)



class CoreFarmError(RuntimeWarning): pass
class AccessForbiddenError(CoreFarmError): pass


class Farm(object):

	def __init__(self, login, key, output_type, ghz):
		self._login = login
		self._key = key
		self._ghz = ghz
		self._output_type = output_type
		self._log = logging.getLogger('yafaray.export')


	def render(self, xml_filename):
		try:
			Blender.Window.DrawProgressBar(0.0, "Packing the data ...")
			datafile = self._pack_data(xml_filename)

			Blender.Window.DrawProgressBar(0.25, "Creating a job ...")
			job_id = self._get_new_job()

			if job_id:
				Blender.Window.DrawProgressBar(0.5, "Uploading the data ...")
				self._upload(job_id, datafile)

				Blender.Window.DrawProgressBar(0.75, "Start rendering on the farm ...")
				self._start_job(job_id)

				Blender.Window.DrawProgressBar(1.0, "Done")
				self._log.debug('Done')
		except urllib2.HTTPError, e:
			self._log.exception('HTTPError: %r' % e.read())
			Blender.Draw.PupMenu('Service is unavailable, please, try later.')
		except IOError, e:
			Blender.Window.DrawProgressBar(1.0, "Done with warning")
			if e.errno == 'socket error':
				Blender.Draw.PupMenu('Service is unavailable, please try later.')
			else:
				raise
		except CoreFarmError, e:
			Blender.Draw.PupMenu(unicode(e))
			Blender.Window.DrawProgressBar(1.0, "Done with warning")
			if isinstance(e, AccessForbiddenError):
				""" Reraise exception to handle it in the event loop.
				"""
				raise
		except Exception:
			self._log.exception('render failed')
			Blender.Window.DrawProgressBar(1.0, "Done with error")
			raise


	def _sign(self, method, data):
		""" Returns the same data dict with
			two additional items: login and signature
		"""
		data = data.copy()
		joined = method + ''.join(
			'='.join(item)
			for item in sorted(data.items())
		) + self._login + self._key

		self._log.debug('Joined data to hash: %r' % joined)

		hash = sha256(joined)
		data['login'] = self._login
		data['signature'] = hash.hexdigest()
		return data


	def _get_new_job(self):
		self._log.debug('Getting a new job id.')
		socket.setdefaulttimeout(10)
		method = 'new_job'
		parameters = self._sign(
			method,
			dict(
				application = 'yafaray',
				date = str(int(time.time())),
				isconfidential = 'true',
			)
		)
		url = '%s%s?%s' % (
			COREFARM_API,
			method,
			urllib.urlencode(parameters)
		)

		self._log.debug('Fetching the URL: %r' % url)
		request = urllib2.Request(url, headers = {'User-Agent': USER_AGENT})
		result = opener.open(request).read()

		self._log.debug('Result is: %r' % result)
		result = simplejson.loads(result)

		if 'msg' in result:
			if 'forbidden' in result['msg'].lower():
				raise AccessForbiddenError(result['msg'])
			else:
				raise CoreFarmError(result['msg'])
		elif 'id' in result:
			return result['id']
		else:
			raise RuntimeError('Unknown result from the server')


	def _start_job(self, job_id):
		self._log.debug('Starting the job: %r' % job_id)
		socket.setdefaulttimeout(10)
		method = 'start_job'
		parameters = self._sign(
			method,
			dict(
				job_id = str(job_id),
				ghz = str(self._ghz),
				date = str(int(time.time())),
				input = '$scene.xml$ %s' % self._output_type,
			)
		)
		url = '%s%s?%s' % (
			COREFARM_API,
			method,
			urllib.urlencode(parameters)
		)

		self._log.debug('Fetching the URL: %r' % url)
		request = urllib2.Request(url, headers = {'User-Agent': USER_AGENT})
		result = opener.open(request).read()

		self._log.debug('Result is: %r' % result)
		result = simplejson.loads(result)

		if 'msg' in result:
			if result['msg'] == 'success':
				Blender.Draw.PupMenu('Your job is now running. You can track its status from your manager on www.corefarm.com; you will also receive an email when it is completed. Thanks!')
			else:
				raise CoreFarmError(result['msg'])
		else:
			raise RuntimeError('Unknown result from the server')


	def _upload(self, job_id, datafile):
		self._log.debug('Uploading file to S3')

		with open(datafile, 'rb') as f:
			headers = {
				'User-Agent': USER_AGENT,
			}
			data = dict(
				key = '%s/${filename}' % job_id,
				redirect = 'http://www.corefarm.com/submit/yafaray',
				acl = 'public-read',
				file = f,
			)
			request = urllib2.Request(S3_HOST, data = data, headers = headers)
			response = opener.open(request)

		if response and response.code != 200:
			self._log.debug('Response from S3: %r' % response)


	def _pack_data(self, xml_filename):
		self._log.debug('Packing filename %r along with textures.' % (xml_filename,))
		start = time.time()

		base_xml_filename = xml_filename.rsplit(os.path.extsep, 1)[0]
		relative_xml_filename = base_xml_filename + '-relative.xml'

		files = [
			(relative_xml_filename, 'scene.xml')
		]

		def make_path_local(match):
			full_path = match.group(2)
			local_path = os.path.basename(full_path)
			files.append((full_path, local_path))
			return match.group(1) + local_path

		with open(xml_filename) as input:
			with open(relative_xml_filename, 'w') as output:
				for line in input:
					output.write(
						re.sub(
							r'(filename sval=")([^"]*)',
							make_path_local,
							line
						)
					)
		tar_filename = base_xml_filename + '.tar.gz'
		tar = tarfile.TarFile.open(tar_filename, mode = 'w|gz')

		try:
			for full_path, archive_path in files:
				tar.add(full_path, archive_path, recursive = False)
		finally:
			tar.close()

		self._log.debug('Packing done in %s seconds' % (time.time() - start))
		return tar_filename


