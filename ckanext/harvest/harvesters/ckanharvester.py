import urllib2

from ckan.lib.base import c
from ckan import model
from ckan.model import Session, Package
from ckan.logic import ValidationError, NotFound, get_action
from ckan.lib.helpers import json

from ckanext.harvest.model import HarvestJob, HarvestObject, HarvestGatherError, \
                                    HarvestObjectError

from ckanclient import CkanClient

import logging
log = logging.getLogger(__name__)

from base import HarvesterBase

class CKANHarvester(HarvesterBase):
    '''
    A Harvester for CKAN instances
    '''
    config = None

    api_version = '2'

    def _get_rest_api_offset(self):
        return '/api/%s/rest' % self.api_version

    def _get_search_api_offset(self):
        return '/api/%s/search' % self.api_version

    def _get_content(self, url):
        http_request = urllib2.Request(
            url = url,
        )

        try:
            api_key = self.config.get('api_key',None)
            if api_key:
                http_request.add_header('Authorization',api_key)
            http_response = urllib2.urlopen(http_request)

            return http_response.read()
        except Exception, e:
            raise e

    def _set_config(self,config_str):
        if config_str:
            self.config = json.loads(config_str)

            if 'api_version' in self.config:
                self.api_version = self.config['api_version']

            log.debug('Using config: %r', self.config)
        else:
            self.config = {}

    def info(self):
        return {
            'name': 'ckan',
            'title': 'CKAN',
            'description': 'Harvests remote CKAN instances',
            'form_config_interface':'Text'
        }

    def validate_config(self,config):
        if not config:
            return config

        try:
            config_obj = json.loads(config)

            if 'default_tags' in config_obj:
                if not isinstance(config_obj['default_tags'],list):
                    raise ValueError('default_tags must be a list')

            if 'default_groups' in config_obj:
                if not isinstance(config_obj['default_groups'],list):
                    raise ValueError('default_groups must be a list')

                # Check if default groups exist
                context = {'model':model,'user':c.user}
                for group_name in config_obj['default_groups']:
                    try:
                        group = get_action('group_show')(context,{'id':group_name})
                    except NotFound,e:
                        raise ValueError('Default group not found')

            if 'default_extras' in config_obj:
                if not isinstance(config_obj['default_extras'],dict):
                    raise ValueError('default_extras must be a dictionary')

            if 'user' in config_obj:
                # Check if user exists
                context = {'model':model,'user':c.user}
                try:
                    user = get_action('user_show')(context,{'id':config_obj.get('user')})
                except NotFound,e:
                    raise ValueError('User not found')

            for key in ('read_only','force_all'):
                if key in config_obj:
                    if not isinstance(config_obj[key],bool):
                        raise ValueError('%s must be boolean' % key)

        except ValueError,e:
            raise e

        return config


    def gather_stage(self,harvest_job):
        log.debug('In CKANHarvester gather_stage (%s)' % harvest_job.source.url)
        get_all_packages = True
        package_ids = []

        self._set_config(harvest_job.source.config)

        # Check if this source has been harvested before
        previous_job = Session.query(HarvestJob) \
                        .filter(HarvestJob.source==harvest_job.source) \
                        .filter(HarvestJob.gather_finished!=None) \
                        .filter(HarvestJob.id!=harvest_job.id) \
                        .order_by(HarvestJob.gather_finished.desc()) \
                        .limit(1).first()

        # Get source URL
        base_url = harvest_job.source.url.rstrip('/')
        base_rest_url = base_url + self._get_rest_api_offset()
        base_search_url = base_url + self._get_search_api_offset()

        if (previous_job and not previous_job.gather_errors and not len(previous_job.objects) == 0):
            if not self.config.get('force_all',False):
                get_all_packages = False

                # Request only the packages modified since last harvest job
                last_time = harvest_job.gather_started.isoformat()
                url = base_search_url + '/revision?since_time=%s' % last_time

                try:
                    content = self._get_content(url)

                    revision_ids = json.loads(content)
                    if len(revision_ids):
                        for revision_id in revision_ids:
                            url = base_rest_url + '/revision/%s' % revision_id
                            try:
                                content = self._get_content(url)
                            except Exception,e:
                                self._save_gather_error('Unable to get content for URL: %s: %s' % (url, str(e)),harvest_job)
                                continue

                            revision = json.loads(content)
                            for package_id in revision.packages:
                                if not package_id in package_ids:
                                    package_ids.append(package_id)
                    else:
                        log.info('No packages have been updated on the remote CKAN instance since the last harvest job')
                        return None

                except urllib2.HTTPError,e:
                    if e.getcode() == 400:
                        log.info('CKAN instance %s does not suport revision filtering' % base_url)
                        get_all_packages = True
                    else:
                        self._save_gather_error('Unable to get content for URL: %s: %s' % (url, str(e)),harvest_job)
                        return None



        if get_all_packages:
            # Request all remote packages
            url = base_rest_url + '/package'
            try:
                content = self._get_content(url)
            except Exception,e:
                self._save_gather_error('Unable to get content for URL: %s: %s' % (url, str(e)),harvest_job)
                return None

            package_ids = json.loads(content)

        try:
            object_ids = []
            if len(package_ids):
                for package_id in package_ids:
                    # Create a new HarvestObject for this identifier
                    obj = HarvestObject(guid = package_id, job = harvest_job)
                    obj.save()
                    object_ids.append(obj.id)

                return object_ids

            else:
               self._save_gather_error('No packages received for URL: %s' % url,
                       harvest_job)
               return None
        except Exception, e:
            self._save_gather_error('%r'%e.message,harvest_job)


    def fetch_stage(self,harvest_object):
        log.debug('In CKANHarvester fetch_stage')

        self._set_config(harvest_object.job.source.config)

        # Get source URL
        url = harvest_object.source.url.rstrip('/')
        url = url + self._get_rest_api_offset() + '/package/' + harvest_object.guid

        # Get contents
        try:
            content = self._get_content(url)
        except Exception,e:
            self._save_object_error('Unable to get content for package: %s: %r' % \
                                        (url, e),harvest_object)
            return None

        # Save the fetched contents in the HarvestObject
        harvest_object.content = content
        harvest_object.save()
        return True

    def import_stage(self,harvest_object):
        log.debug('In CKANHarvester import_stage')
        if not harvest_object:
            log.error('No harvest object received')
            return False

        if harvest_object.content is None:
            self._save_object_error('Empty content for object %s' % harvest_object.id,
                    harvest_object, 'Import')
            return False

        self._set_config(harvest_object.job.source.config)

        try:
            package_dict = json.loads(harvest_object.content)

            # Set default tags if needed
            default_tags = self.config.get('default_tags',[])
            if default_tags:
                if not 'tags' in package_dict:
                    package_dict['tags'] = []
                package_dict['tags'].extend([t for t in default_tags if t not in package_dict['tags']])

            # Ignore remote groups for the time being
            del package_dict['groups']

            # Set default groups if needed
            default_groups = self.config.get('default_groups',[])
            if default_groups:
                if not 'groups' in package_dict:
                    package_dict['groups'] = []
                package_dict['groups'].extend([g for g in default_groups if g not in package_dict['groups']])

            # Set default extras if needed
            default_extras = self.config.get('default_extras',{})
            if default_extras:
                override_extras = self.config.get('override_extras',False)
                if not 'extras' in package_dict:
                    package_dict['extras'] = {}
                for key,value in default_extras.iteritems():
                    if not key in package_dict['extras'] or override_extras:
                        # Look for replacement strings
                        if isinstance(value,basestring):
                            value = value.format(harvest_source_id=harvest_object.job.source.id,
                                     harvest_source_url=harvest_object.job.source.url.strip('/'),
                                     harvest_source_title=harvest_object.job.source.title,
                                     harvest_job_id=harvest_object.job.id,
                                     harvest_object_id=harvest_object.id,
                                     dataset_id=package_dict['id'])

                        package_dict['extras'][key] = value

            result = self._create_or_update_package(package_dict,harvest_object)

            if result and self.config.get('read_only',False) == True:

                package = model.Package.get(package_dict['id'])

                # Clear default permissions
                model.clear_user_roles(package)

                # Setup harvest user as admin
                user_name = self.config.get('user',u'harvest')
                user = model.User.get(user_name)
                pkg_role = model.PackageRole(package=package, user=user, role=model.Role.ADMIN)

                # Other users can only read
                for user_name in (u'visitor',u'logged_in'):
                    user = model.User.get(user_name)
                    pkg_role = model.PackageRole(package=package, user=user, role=model.Role.READER)


        except ValidationError,e:
            self._save_object_error('Invalid package with GUID %s: %r' % (harvest_object.guid, e.error_dict),
                    harvest_object, 'Import')
        except Exception, e:
            self._save_object_error('%r'%e,harvest_object,'Import')

