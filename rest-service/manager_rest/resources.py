#########
# Copyright (c) 2013 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#  * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  * See the License for the specific language governing permissions and
#  * limitations under the License.
#

import os
import zipfile
import urllib
import tempfile
import shutil
import uuid
import contextlib
from functools import wraps
from os import path
from urllib2 import urlopen, URLError

from setuptools import archive_util

import elasticsearch
from flask import (
    request,
    make_response,
    current_app as app
)
from flask.ext.restful import Resource, marshal, reqparse
from flask_restful_swagger import swagger
from flask.ext.restful.utils import unpack
from flask_securest.rest_security import SECURED_MODE, SecuredResource

from manager_rest import config
from manager_rest import models
from manager_rest import responses
from manager_rest import requests_schema
from manager_rest import chunked
from manager_rest import archiving
from manager_rest import manager_exceptions
from manager_rest import utils
from manager_rest.storage_manager import get_storage_manager
from manager_rest.blueprints_manager import (DslParseException,
                                             get_blueprints_manager)
from manager_rest import get_version_data

CONVENTION_APPLICATION_BLUEPRINT_FILE = 'blueprint.yaml'

SUPPORTED_ARCHIVE_TYPES = ['zip', 'tar', 'tar.gz', 'tar.bz2']


def exceptions_handled(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except manager_exceptions.ManagerException as e:
            utils.abort_error(e, app.logger)
    return wrapper


def _get_fields_to_include(model_fields):
    if '_include' in request.args and request.args['_include']:
        include = set(request.args['_include'].split(','))
        include_fields = {}
        illegal_fields = None
        for field in include:
            if field not in model_fields:
                if not illegal_fields:
                    illegal_fields = []
                illegal_fields.append(field)
                continue
            include_fields[field] = model_fields[field]
        if illegal_fields:
            raise manager_exceptions.NoSuchIncludeFieldError(
                'Illegal include fields: [{}] - available fields: '
                '[{}]'.format(', '.join(illegal_fields),
                              ', '.join(model_fields.keys())))
        return include_fields
    return model_fields


class marshal_with(object):
    def __init__(self, fields):
        """
        :param fields: Model resource fields to marshal result according to.
        """
        self.fields = fields

    def __call__(self, f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            include = _get_fields_to_include(self.fields)
            response = f(*args, **kwargs)
            if isinstance(response, tuple):
                data, code, headers = unpack(response)
                return marshal(data, include), code, headers
            else:
                return marshal(response, include)
        return wrapper


def verify_json_content_type():
    if request.content_type != 'application/json':
        raise manager_exceptions.UnsupportedContentTypeError(
            'Content type must be application/json')


def verify_parameter_in_request_body(param,
                                     request_json,
                                     param_type=None,
                                     optional=False):
    if param not in request_json:
        if optional:
            return
        raise manager_exceptions.BadParametersError(
            'Missing {0} in json request body'.format(param))
    if param_type and not isinstance(request_json[param], param_type):
        raise manager_exceptions.BadParametersError(
            '{0} parameter is expected to be of type {1} but is of type '
            '{2}'.format(param,
                         param_type.__name__,
                         type(request_json[param]).__name__))


def verify_and_convert_bool(attribute_name, str_bool):
    if isinstance(str_bool, bool):
        return str_bool
    if str_bool.lower() == 'true':
        return True
    if str_bool.lower() == 'false':
        return False
    raise manager_exceptions.BadParametersError(
        '{0} must be <true/false>, got {1}'.format(attribute_name, str_bool))


def _replace_workflows_field_for_deployment_response(deployment_dict):
    deployment_workflows = deployment_dict['workflows']
    if deployment_workflows is not None:
        workflows = [responses.Workflow(
            name=wf_name, created_at=None, parameters=wf.get(
                'parameters', dict())) for wf_name, wf
            in deployment_workflows.iteritems()]

        deployment_dict['workflows'] = workflows
    return deployment_dict


def setup_resources(api):
    api = swagger.docs(api,
                       apiVersion='0.1',
                       basePath='http://localhost:8100')
    api.add_resource(Blueprints, '/blueprints')
    api.add_resource(BlueprintsId, '/blueprints/<string:blueprint_id>')
    api.add_resource(BlueprintsIdArchive,
                     '/blueprints/<string:blueprint_id>/archive')
    api.add_resource(Executions, '/executions')
    api.add_resource(ExecutionsId, '/executions/<string:execution_id>')
    api.add_resource(Deployments, '/deployments')
    api.add_resource(DeploymentsId, '/deployments/<string:deployment_id>')
    api.add_resource(DeploymentsIdOutputs,
                     '/deployments/<string:deployment_id>/outputs')
    api.add_resource(DeploymentModifications,
                     '/deployment-modifications')
    api.add_resource(DeploymentModificationsId,
                     '/deployment-modifications/<string:modification_id>')
    api.add_resource(
        DeploymentModificationsIdFinish,
        '/deployment-modifications/<string:modification_id>/finish')
    api.add_resource(
        DeploymentModificationsIdRollback,
        '/deployment-modifications/<string:modification_id>/rollback')
    api.add_resource(Nodes, '/nodes')
    api.add_resource(NodeInstances, '/node-instances')
    api.add_resource(NodeInstancesId,
                     '/node-instances/<string:node_instance_id>')
    api.add_resource(Events, '/events')
    api.add_resource(Search, '/search')
    api.add_resource(Status, '/status')
    api.add_resource(ProviderContext, '/provider/context')
    api.add_resource(Version, '/version')
    api.add_resource(EvaluateFunctions, '/evaluate/functions')
    api.add_resource(Tokens, '/tokens')


class BlueprintsUpload(object):
    def do_request(self, blueprint_id):
        file_server_root = config.instance().file_server_root
        archive_target_path = tempfile.mktemp(dir=file_server_root)
        try:
            self._save_file_locally(archive_target_path)
            application_dir = self._extract_file_to_file_server(
                file_server_root, archive_target_path)
            blueprint = self._prepare_and_submit_blueprint(file_server_root,
                                                           application_dir,
                                                           blueprint_id)
            self._move_archive_to_uploaded_blueprints_dir(blueprint.id,
                                                          file_server_root,
                                                          archive_target_path)
            return blueprint, 201
        finally:
            if os.path.exists(archive_target_path):
                os.remove(archive_target_path)

    @staticmethod
    def _move_archive_to_uploaded_blueprints_dir(blueprint_id,
                                                 file_server_root,
                                                 archive_path):
        if not os.path.exists(archive_path):
            raise RuntimeError("Archive [{0}] doesn't exist - Cannot move "
                               "archive to uploaded blueprints "
                               "directory".format(archive_path))
        uploaded_blueprint_dir = os.path.join(
            file_server_root,
            config.instance().file_server_uploaded_blueprints_folder,
            blueprint_id)
        os.makedirs(uploaded_blueprint_dir)
        archive_type = archiving.get_archive_type(archive_path)
        archive_file_name = '{0}.{1}'.format(blueprint_id, archive_type)
        shutil.move(archive_path,
                    os.path.join(uploaded_blueprint_dir, archive_file_name))

    def _process_plugins(self, file_server_root, blueprint_id):
        plugins_directory = path.join(file_server_root,
                                      "blueprints", blueprint_id, "plugins")
        if not path.isdir(plugins_directory):
            return
        plugins = [path.join(plugins_directory, directory)
                   for directory in os.listdir(plugins_directory)
                   if path.isdir(path.join(plugins_directory, directory))]

        for plugin_dir in plugins:
            final_zip_name = '{0}.zip'.format(path.basename(plugin_dir))
            target_zip_path = path.join(file_server_root,
                                        "blueprints", blueprint_id,
                                        'plugins', final_zip_name)
            self._zip_dir(plugin_dir, target_zip_path)

    @staticmethod
    def _zip_dir(dir_to_zip, target_zip_path):
        zipf = zipfile.ZipFile(target_zip_path, 'w', zipfile.ZIP_DEFLATED)
        try:
            plugin_dir_base_name = path.basename(dir_to_zip)
            rootlen = len(dir_to_zip) - len(plugin_dir_base_name)
            for base, dirs, files in os.walk(dir_to_zip):
                for entry in files:
                    fn = os.path.join(base, entry)
                    zipf.write(fn, fn[rootlen:])
        finally:
            zipf.close()

    @staticmethod
    def _save_file_locally(archive_file_name):

        if 'blueprint_archive_url' in request.args:

            if request.data or 'Transfer-Encoding' in request.headers:
                raise manager_exceptions.BadParametersError(
                    "Can't pass both a blueprint URL via query parameters "
                    "and blueprint data via the request body at the same time")

            blueprint_url = request.args['blueprint_archive_url']
            try:
                with contextlib.closing(urlopen(blueprint_url)) as urlf:
                    with open(archive_file_name, 'w') as f:
                        f.write(urlf.read())
                return
            except URLError:
                raise manager_exceptions.ParamUrlNotFoundError(
                    "URL {0} not found - can't download blueprint archive"
                    .format(blueprint_url))
            except ValueError:
                raise manager_exceptions.BadParametersError(
                    "URL {0} is malformed - can't download blueprint archive"
                    .format(blueprint_url))

        # save uploaded file
        if 'Transfer-Encoding' in request.headers:
            with open(archive_file_name, 'w') as f:
                for buffered_chunked in chunked.decode(request.input_stream):
                    f.write(buffered_chunked)
        else:
            if not request.data:
                raise manager_exceptions.BadParametersError(
                    'Missing application archive in request body or '
                    '"blueprint_archive_url" in query parameters')
            uploaded_file_data = request.data
            with open(archive_file_name, 'w') as f:
                f.write(uploaded_file_data)

    @staticmethod
    def _extract_file_to_file_server(file_server_root,
                                     archive_target_path):
        # extract application to file server
        tempdir = tempfile.mkdtemp('-blueprint-submit')
        try:
            try:
                archive_util.unpack_archive(archive_target_path, tempdir)
            except archive_util.UnrecognizedFormat:
                raise manager_exceptions.BadParametersError(
                    'Blueprint archive is of an unrecognized format. '
                    'Supported formats are: {0}'.format(
                        SUPPORTED_ARCHIVE_TYPES))
            archive_file_list = os.listdir(tempdir)
            if len(archive_file_list) != 1 or not path.isdir(
                    path.join(tempdir, archive_file_list[0])):
                raise manager_exceptions.BadParametersError(
                    'archive must contain exactly 1 directory')
            application_dir_base_name = archive_file_list[0]
            # generating temporary unique name for app dir, to allow multiple
            # uploads of apps with the same name (as it appears in the file
            # system, not the app name field inside the blueprint.
            # the latter is guaranteed to be unique).
            generated_app_dir_name = '{0}-{1}'.format(
                application_dir_base_name, uuid.uuid4())
            temp_application_dir = path.join(tempdir,
                                             application_dir_base_name)
            temp_application_target_dir = path.join(tempdir,
                                                    generated_app_dir_name)
            shutil.move(temp_application_dir, temp_application_target_dir)
            shutil.move(temp_application_target_dir, file_server_root)
            return generated_app_dir_name
        finally:
            shutil.rmtree(tempdir)

    def _prepare_and_submit_blueprint(self, file_server_root,
                                      application_dir,
                                      blueprint_id):
        application_file = self._extract_application_file(file_server_root,
                                                          application_dir)

        file_server_base_url = config.instance().file_server_base_uri
        dsl_path = '{0}/{1}'.format(file_server_base_url, application_file)
        resources_base = file_server_base_url + '/'

        # add to blueprints manager (will also dsl_parse it)
        try:
            blueprint = get_blueprints_manager().publish_blueprint(
                dsl_path, resources_base, blueprint_id)

            # moving the app directory in the file server to be under a
            # directory named after the blueprint id
            shutil.move(os.path.join(file_server_root, application_dir),
                        os.path.join(
                            file_server_root,
                            config.instance().file_server_blueprints_folder,
                            blueprint.id))
            self._process_plugins(file_server_root, blueprint.id)
            return blueprint
        except DslParseException, ex:
            shutil.rmtree(os.path.join(file_server_root, application_dir))
            raise manager_exceptions.InvalidBlueprintError(
                'Invalid blueprint - {0}'.format(ex.message))

    @staticmethod
    def _extract_application_file(file_server_root, application_dir):

        full_application_dir = path.join(file_server_root, application_dir)

        if 'application_file_name' in request.args:
            application_file_name = urllib.unquote(
                request.args['application_file_name']).decode('utf-8')
            application_file = path.join(full_application_dir,
                                         application_file_name)
            if not path.isfile(application_file):
                raise manager_exceptions.BadParametersError(
                    '{0} does not exist in the application '
                    'directory'.format(application_file_name)
                )
        else:
            application_file_name = CONVENTION_APPLICATION_BLUEPRINT_FILE
            application_file = path.join(full_application_dir,
                                         application_file_name)
            if not path.isfile(application_file):
                raise manager_exceptions.BadParametersError(
                    'application directory is missing blueprint.yaml and '
                    'application_file_name query parameter was not passed')

        # return relative path from the file server root since this path
        # is appended to the file server base uri
        return path.join(application_dir, application_file_name)


class BlueprintsIdArchive(SecuredResource):

    @swagger.operation(
        nickname="getArchive",
        notes="Downloads blueprint as an archive."
    )
    @exceptions_handled
    def get(self, blueprint_id):
        """
        Download blueprint's archive
        """
        # Verify blueprint exists.
        get_blueprints_manager().get_blueprint(blueprint_id, {'id'})

        for arc_type in SUPPORTED_ARCHIVE_TYPES:
            # attempting to find the archive file on the file system
            local_path = os.path.join(
                config.instance().file_server_root,
                config.instance().file_server_uploaded_blueprints_folder,
                blueprint_id,
                '{0}.{1}'.format(blueprint_id, arc_type))

            if os.path.isfile(local_path):
                archive_type = arc_type
                break
        else:
            raise RuntimeError("Could not find blueprint's archive; "
                               "Blueprint ID: {0}".format(blueprint_id))

        blueprint_path = '{0}/{1}/{2}/{2}.{3}'.format(
            config.instance().file_server_resources_uri,
            config.instance().file_server_uploaded_blueprints_folder,
            blueprint_id,
            archive_type)

        response = make_response()
        response.headers['Content-Description'] = 'File Transfer'
        response.headers['Cache-Control'] = 'no-cache'
        response.headers['Content-Type'] = 'application/octet-stream'
        response.headers['Content-Disposition'] = \
            'attachment; filename={0}.{1}'.format(blueprint_id, archive_type)
        response.headers['Content-Length'] = os.path.getsize(local_path)
        response.headers['X-Accel-Redirect'] = blueprint_path
        response.headers['X-Accel-Buffering'] = 'yes'
        return response


class Blueprints(SecuredResource):

    @swagger.operation(
        responseClass='List[{0}]'.format(responses.BlueprintState.__name__),
        nickname="list",
        notes="Returns a list a submitted blueprints."
    )
    @exceptions_handled
    @marshal_with(responses.BlueprintState.resource_fields)
    def get(self, _include=None):
        """
        List uploaded blueprints
        """

        return get_blueprints_manager().blueprints_list(_include)


class BlueprintsId(SecuredResource):

    @swagger.operation(
        responseClass=responses.BlueprintState,
        nickname="getById",
        notes="Returns a blueprint by its id."
    )
    @exceptions_handled
    @marshal_with(responses.BlueprintState.resource_fields)
    def get(self, blueprint_id, _include=None):
        """
        Get blueprint by id
        """
        blueprint = get_blueprints_manager().get_blueprint(blueprint_id,
                                                           _include)
        return responses.BlueprintState(**blueprint.to_dict())

    @swagger.operation(
        responseClass=responses.BlueprintState,
        nickname="upload",
        notes="Submitted blueprint should be an archive "
              "containing the directory which contains the blueprint. "
              "Archive format may be zip, tar, tar.gz or tar.bz2."
              " Blueprint archive may be submitted via either URL or by "
              "direct upload.",
        parameters=[{'name': 'application_file_name',
                     'description': 'File name of yaml '
                                    'containing the "main" blueprint.',
                     'required': False,
                     'allowMultiple': False,
                     'dataType': 'string',
                     'paramType': 'query',
                     'defaultValue': 'blueprint.yaml'},
                    {'name': 'blueprint_archive_url',
                     'description': 'url of a blueprint archive file',
                     'required': False,
                     'allowMultiple': False,
                     'dataType': 'string',
                     'paramType': 'query'},
                    {
                        'name': 'body',
                        'description': 'Binary form of the tar '
                                       'gzipped blueprint directory',
                        'required': True,
                        'allowMultiple': False,
                        'dataType': 'binary',
                        'paramType': 'body'}],
        consumes=[
            "application/octet-stream"
        ]

    )
    @exceptions_handled
    @marshal_with(responses.BlueprintState.resource_fields)
    def put(self, blueprint_id):
        """
        Upload a blueprint (id specified)
        """
        return BlueprintsUpload().do_request(blueprint_id=blueprint_id)

    @swagger.operation(
        responseClass=responses.BlueprintState,
        nickname="deleteById",
        notes="deletes a blueprint by its id."
    )
    @exceptions_handled
    @marshal_with(responses.BlueprintState.resource_fields)
    def delete(self, blueprint_id):
        """
        Delete blueprint by id
        """
        # Note: The current delete semantics are such that if a deployment
        # for the blueprint exists, the deletion operation will fail.
        # However, there is no handling of possible concurrency issue with
        # regard to that matter at the moment.
        blueprint = get_blueprints_manager().delete_blueprint(blueprint_id)

        # Delete blueprint resources from file server
        blueprint_folder = os.path.join(
            config.instance().file_server_root,
            config.instance().file_server_blueprints_folder,
            blueprint.id)
        shutil.rmtree(blueprint_folder)
        uploaded_blueprint_folder = os.path.join(
            config.instance().file_server_root,
            config.instance().file_server_uploaded_blueprints_folder,
            blueprint.id)
        shutil.rmtree(uploaded_blueprint_folder)

        return responses.BlueprintState(**blueprint.to_dict()), 200


class Executions(SecuredResource):

    @swagger.operation(
        responseClass='List[{0}]'.format(responses.Execution.__name__),
        nickname="list",
        notes="Returns a list of executions for the optionally provided "
              "deployment id."
    )
    @exceptions_handled
    @marshal_with(responses.Execution.resource_fields)
    def get(self, _include=None):
        """List executions"""
        deployment_id = request.args.get('deployment_id')
        if deployment_id:
            get_blueprints_manager().get_deployment(deployment_id,
                                                    include=['id'])
        executions = get_blueprints_manager().executions_list(
            deployment_id=deployment_id, include=_include)
        return [responses.Execution(**e.to_dict()) for e in executions]

    @exceptions_handled
    @marshal_with(responses.Execution.resource_fields)
    def post(self):
        """Execute a workflow"""
        verify_json_content_type()
        request_json = request.json
        verify_parameter_in_request_body('deployment_id', request_json)
        verify_parameter_in_request_body('workflow_id', request_json)

        allow_custom_parameters = verify_and_convert_bool(
            'allow_custom_parameters',
            request_json.get('allow_custom_parameters', 'false'))
        force = verify_and_convert_bool(
            'force',
            request_json.get('force', 'false'))

        deployment_id = request.json['deployment_id']
        workflow_id = request.json['workflow_id']
        parameters = request.json.get('parameters', None)

        if parameters is not None and parameters.__class__ is not dict:
            raise manager_exceptions.BadParametersError(
                "request body's 'parameters' field must be a dict but"
                " is of type {0}".format(parameters.__class__.__name__))

        execution = get_blueprints_manager().execute_workflow(
            deployment_id, workflow_id, parameters=parameters,
            allow_custom_parameters=allow_custom_parameters, force=force)
        return responses.Execution(**execution.to_dict()), 201


class ExecutionsId(SecuredResource):

    @swagger.operation(
        responseClass=responses.Execution,
        nickname="getById",
        notes="Returns the execution state by its id.",
    )
    @exceptions_handled
    @marshal_with(responses.Execution.resource_fields)
    def get(self, execution_id, _include=None):
        """
        Get execution by id
        """
        execution = get_blueprints_manager().get_execution(execution_id,
                                                           include=_include)
        return responses.Execution(**execution.to_dict())

    @swagger.operation(
        responseClass=responses.Execution,
        nickname="modify_state",
        notes="Modifies a running execution state (currently, only cancel"
              " and force-cancel are supported)",
        parameters=[{'name': 'body',
                     'description': 'json with an action key. '
                                    'Legal values for action are: [cancel,'
                                    ' force-cancel]',
                     'required': True,
                     'allowMultiple': False,
                     'dataType': requests_schema.ModifyExecutionRequest.__name__,  # NOQA
                     'paramType': 'body'}],
        consumes=[
            "application/json"
        ]
    )
    @exceptions_handled
    @marshal_with(responses.Execution.resource_fields)
    def post(self, execution_id):
        """
        Apply execution action (cancel, force-cancel) by id
        """
        verify_json_content_type()
        request_json = request.json
        verify_parameter_in_request_body('action', request_json)
        action = request.json['action']

        valid_actions = ['cancel', 'force-cancel']

        if action not in valid_actions:
            raise manager_exceptions.BadParametersError(
                'Invalid action: {0}, Valid action values are: {1}'.format(
                    action, valid_actions))

        if action in ('cancel', 'force-cancel'):
            return get_blueprints_manager().cancel_execution(
                execution_id, action == 'force-cancel')

    @swagger.operation(
        responseClass=responses.Execution,
        nickname="updateExecutionStatus",
        notes="Updates the execution's status",
        parameters=[{'name': 'status',
                     'description': "The execution's new status",
                     'required': True,
                     'allowMultiple': False,
                     'dataType': 'string',
                     'paramType': 'body'},
                    {'name': 'error',
                     'description': "An error message. If omitted, "
                                    "error will be updated to an empty "
                                    "string",
                     'required': False,
                     'allowMultiple': False,
                     'dataType': 'string',
                     'paramType': 'body'}],
        consumes=[
            "application/json"
        ]
    )
    @exceptions_handled
    @marshal_with(responses.Execution.resource_fields)
    def patch(self, execution_id):
        """
        Update execution status by id
        """
        verify_json_content_type()
        request_json = request.json
        verify_parameter_in_request_body('status', request_json)

        get_storage_manager().update_execution_status(
            execution_id,
            request_json['status'],
            request_json.get('error', ''))

        return responses.Execution(**get_storage_manager().get_execution(
            execution_id).to_dict())


class Deployments(SecuredResource):

    @swagger.operation(
        responseClass='List[{0}]'.format(responses.Deployment.__name__),
        nickname="list",
        notes="Returns a list existing deployments."
    )
    @exceptions_handled
    @marshal_with(responses.Deployment.resource_fields)
    def get(self, _include=None):
        """
        List deployments
        """
        deployments = get_blueprints_manager().deployments_list(
            include=_include)
        return [
            responses.Deployment(
                **_replace_workflows_field_for_deployment_response(
                    d.to_dict()))
            for d in deployments
        ]


class DeploymentsId(SecuredResource):

    def __init__(self):
        self._args_parser = reqparse.RequestParser()
        self._args_parser.add_argument('ignore_live_nodes', type=str,
                                       default='false', location='args')

    @swagger.operation(
        responseClass=responses.Deployment,
        nickname="getById",
        notes="Returns a deployment by its id."
    )
    @exceptions_handled
    @marshal_with(responses.Deployment.resource_fields)
    def get(self, deployment_id, _include=None):
        """
        Get deployment by id
        """
        deployment = get_blueprints_manager().get_deployment(deployment_id,
                                                             include=_include)
        return responses.Deployment(
            **_replace_workflows_field_for_deployment_response(
                deployment.to_dict()))

    @swagger.operation(
        responseClass=responses.Deployment,
        nickname="createDeployment",
        notes="Created a new deployment of the given blueprint.",
        parameters=[{'name': 'body',
                     'description': 'Deployment blue print',
                     'required': True,
                     'allowMultiple': False,
                     'dataType': requests_schema.DeploymentRequest.__name__,
                     'paramType': 'body'}],
        consumes=[
            "application/json"
        ]
    )
    @exceptions_handled
    @marshal_with(responses.Deployment.resource_fields)
    def put(self, deployment_id):
        """
        Create a deployment
        """
        verify_json_content_type()
        request_json = request.json
        verify_parameter_in_request_body('blueprint_id', request_json)
        verify_parameter_in_request_body('inputs',
                                         request_json,
                                         param_type=dict,
                                         optional=True)
        blueprint_id = request.json['blueprint_id']
        deployment = get_blueprints_manager().create_deployment(
            blueprint_id, deployment_id, inputs=request_json.get('inputs', {}))
        return responses.Deployment(
            **_replace_workflows_field_for_deployment_response(
                deployment.to_dict())), 201

    @swagger.operation(
        responseClass=responses.Deployment,
        nickname="deleteById",
        notes="deletes a deployment by its id.",
        parameters=[{'name': 'ignore_live_nodes',
                     'description': 'Specifies whether to ignore live nodes,'
                                    'or raise an error upon such nodes '
                                    'instead.',
                     'required': False,
                     'allowMultiple': False,
                     'dataType': 'boolean',
                     'defaultValue': False,
                     'paramType': 'query'}]
    )
    @exceptions_handled
    @marshal_with(responses.Deployment.resource_fields)
    def delete(self, deployment_id):
        """
        Delete deployment by id
        """
        args = self._args_parser.parse_args()

        ignore_live_nodes = verify_and_convert_bool(
            'ignore_live_nodes', args['ignore_live_nodes'])

        deployment = get_blueprints_manager().delete_deployment(
            deployment_id, ignore_live_nodes)
        # not using '_replace_workflows_field_for_deployment_response'
        # method since the object returned only contains the deployment's id
        return responses.Deployment(**deployment.to_dict()), 200


class DeploymentModifications(SecuredResource):

    def __init__(self):
        self._args_parser = reqparse.RequestParser()
        self._args_parser.add_argument('deployment_id',
                                       type=str,
                                       required=False,
                                       location='args')

    @swagger.operation(
        responseClass=responses.DeploymentModification,
        nickname="modifyDeployment",
        notes="Modify deployment.",
        parameters=[{'name': 'body',
                     'description': 'Deployment modification specification',
                     'required': True,
                     'allowMultiple': False,
                     'dataType': requests_schema.
                     DeploymentModificationRequest.__name__,
                     'paramType': 'body'}],
        consumes=[
            "application/json"
        ]
    )
    @exceptions_handled
    @marshal_with(responses.DeploymentModification.resource_fields)
    def post(self):
        verify_json_content_type()
        request_json = request.json
        verify_parameter_in_request_body('deployment_id', request_json)
        deployment_id = request_json['deployment_id']
        verify_parameter_in_request_body('context',
                                         request_json,
                                         param_type=dict,
                                         optional=True)
        context = request_json.get('context', {})
        verify_parameter_in_request_body('nodes',
                                         request_json,
                                         param_type=dict,
                                         optional=True)
        nodes = request_json.get('nodes', {})
        modification = get_blueprints_manager().\
            start_deployment_modification(deployment_id, nodes, context)
        return responses.DeploymentModification(**modification.to_dict()), 201

    @swagger.operation(
        responseClass='List[{0}]'.format(
            responses.DeploymentModification.__name__),
        nickname="listDeploymentModifications",
        notes="List deployment modifications.",
        parameters=[{'name': 'deployment_id',
                     'description': 'Deployment id',
                     'required': False,
                     'allowMultiple': False,
                     'dataType': 'string',
                     'paramType': 'query'}]
    )
    @exceptions_handled
    @marshal_with(responses.DeploymentModification.resource_fields)
    def get(self, _include=None):
        args = self._args_parser.parse_args()
        deployment_id = args.get('deployment_id')
        modifications = get_storage_manager().deployment_modifications_list(
            deployment_id, include=_include)
        return [responses.DeploymentModification(**m.to_dict())
                for m in modifications]


class DeploymentModificationsId(SecuredResource):

    @swagger.operation(
        responseClass=responses.DeploymentModification,
        nickname="getDeploymentModification",
        notes="Get deployment modification."
    )
    @exceptions_handled
    @marshal_with(responses.DeploymentModification.resource_fields)
    def get(self, modification_id, _include=None):
        modification = get_storage_manager().get_deployment_modification(
            modification_id, include=_include)
        return responses.DeploymentModification(**modification.to_dict())


class DeploymentModificationsIdFinish(SecuredResource):

    @swagger.operation(
        responseClass=responses.DeploymentModification,
        nickname="finishDeploymentModification",
        notes="Finish deployment modification."
    )
    @exceptions_handled
    @marshal_with(responses.DeploymentModification.resource_fields)
    def post(self, modification_id):
        modification = get_blueprints_manager().finish_deployment_modification(
            modification_id)
        return responses.DeploymentModification(**modification.to_dict())


class DeploymentModificationsIdRollback(SecuredResource):

    @swagger.operation(
        responseClass=responses.DeploymentModification,
        nickname="rollbackDeploymentModification",
        notes="Rollback deployment modification."
    )
    @exceptions_handled
    @marshal_with(responses.DeploymentModification.resource_fields)
    def post(self, modification_id):
        modification = get_blueprints_manager(
            ).rollback_deployment_modification(modification_id)
        return responses.DeploymentModification(**modification.to_dict())


class Nodes(SecuredResource):

    def __init__(self):
        self._args_parser = reqparse.RequestParser()
        self._args_parser.add_argument('deployment_id',
                                       type=str,
                                       required=False,
                                       location='args')
        self._args_parser.add_argument('node_id',
                                       type=str,
                                       required=False,
                                       location='args')

    @swagger.operation(
        responseClass='List[{0}]'.format(responses.Node.__name__),
        nickname="listNodes",
        notes="Returns nodes list according to the provided query parameters.",
        parameters=[{'name': 'deployment_id',
                     'description': 'Deployment id',
                     'required': False,
                     'allowMultiple': False,
                     'dataType': 'string',
                     'paramType': 'query'}]
    )
    @exceptions_handled
    @marshal_with(responses.Node.resource_fields)
    def get(self, _include=None):
        """
        List nodes
        """
        args = self._args_parser.parse_args()
        deployment_id = args.get('deployment_id')
        node_id = args.get('node_id')
        if deployment_id and node_id:
            try:
                nodes = [get_storage_manager().get_node(deployment_id,
                                                        node_id)]
            except manager_exceptions.NotFoundError:
                nodes = []
        else:
            nodes = get_storage_manager().get_nodes(deployment_id,
                                                    include=_include)
        return [responses.Node(**node.to_dict()) for node in nodes]


class NodeInstances(SecuredResource):

    def __init__(self):
        self._args_parser = reqparse.RequestParser()
        self._args_parser.add_argument('deployment_id',
                                       type=str,
                                       required=False,
                                       location='args')
        self._args_parser.add_argument('node_name',
                                       type=str,
                                       required=False,
                                       location='args')

    @swagger.operation(
        responseClass='List[{0}]'.format(responses.NodeInstance.__name__),
        nickname="listNodeInstances",
        notes="Returns node instances list according to the provided query"
              " parameters.",
        parameters=[{'name': 'deployment_id',
                     'description': 'Deployment id',
                     'required': False,
                     'allowMultiple': False,
                     'dataType': 'string',
                     'paramType': 'query'},
                    {'name': 'node_name',
                     'description': 'node name',
                     'required': False,
                     'allowMultiple': False,
                     'dataType': 'string',
                     'paramType': 'query'}]
    )
    @exceptions_handled
    @marshal_with(responses.NodeInstance.resource_fields)
    def get(self, _include=None):
        """
        List node instances
        """
        args = self._args_parser.parse_args()
        deployment_id = args.get('deployment_id')
        node_name = args.get('node_name')
        nodes = get_storage_manager().get_node_instances(deployment_id,
                                                         node_name,
                                                         include=_include)
        return [responses.NodeInstance(**node.to_dict()) for node in nodes]


class NodeInstancesId(SecuredResource):

    @swagger.operation(
        responseClass=responses.Node,
        nickname="getNodeInstance",
        notes="Returns node state/runtime properties "
              "according to the provided query parameters.",
        parameters=[{'name': 'node_id',
                     'description': 'Node Id',
                     'required': True,
                     'allowMultiple': False,
                     'dataType': 'string',
                     'paramType': 'path'},
                    {'name': 'state_and_runtime_properties',
                     'description': 'Specifies whether to return state and '
                                    'runtime properties',
                     'required': False,
                     'allowMultiple': False,
                     'dataType': 'boolean',
                     'defaultValue': True,
                     'paramType': 'query'}]
    )
    @exceptions_handled
    @marshal_with(responses.NodeInstance.resource_fields)
    def get(self, node_instance_id, _include=None):
        """
        Get node instance by id
        """
        instance = get_storage_manager().get_node_instance(node_instance_id,
                                                           include=_include)
        return responses.NodeInstance(
            id=node_instance_id,
            node_id=instance.node_id,
            host_id=instance.host_id,
            relationships=instance.relationships,
            deployment_id=instance.deployment_id,
            state=instance.state,
            runtime_properties=instance.runtime_properties,
            version=instance.version)

    @swagger.operation(
        responseClass=responses.NodeInstance,
        nickname="patchNodeState",
        notes="Update node instance. Expecting the request body to "
              "be a dictionary containing 'version' which is used for "
              "optimistic locking during the update, and optionally "
              "'runtime_properties' (dictionary) and/or 'state' (string) "
              "properties",
        parameters=[{'name': 'node_instance_id',
                     'description': 'Node instance identifier',
                     'required': True,
                     'allowMultiple': False,
                     'dataType': 'string',
                     'paramType': 'path'},
                    {'name': 'version',
                     'description': 'used for optimistic locking during '
                                    'update',
                     'required': True,
                     'allowMultiple': False,
                     'dataType': 'int',
                     'paramType': 'body'},
                    {'name': 'runtime_properties',
                     'description': 'a dictionary of runtime properties. If '
                                    'omitted, the runtime properties wont be '
                                    'updated',
                     'required': False,
                     'allowMultiple': False,
                     'dataType': 'dict',
                     'paramType': 'body'},
                    {'name': 'state',
                     'description': "the new node's state. If omitted, "
                                    "the state wont be updated",
                     'required': False,
                     'allowMultiple': False,
                     'dataType': 'string',
                     'paramType': 'body'}],
        consumes=["application/json"]
    )
    @exceptions_handled
    @marshal_with(responses.NodeInstance.resource_fields)
    def patch(self, node_instance_id):
        """
        Update node instance by id
        """
        verify_json_content_type()
        if request.json.__class__ is not dict or \
                'version' not in request.json or \
                request.json['version'].__class__ is not int:

            if request.json.__class__ is not dict:
                message = 'Request body is expected to be a map containing ' \
                          'a "version" field and optionally ' \
                          '"runtimeProperties" and/or "state" fields'
            elif 'version' not in request.json:
                message = 'Request body must be a map containing a ' \
                          '"version" field'
            else:
                message = \
                    "request body's 'version' field must be an int but" \
                    " is of type {0}".format(request.json['version']
                                             .__class__.__name__)
            raise manager_exceptions.BadParametersError(message)

        node = models.DeploymentNodeInstance(
            id=node_instance_id,
            node_id=None,
            relationships=None,
            host_id=None,
            deployment_id=None,
            runtime_properties=request.json.get('runtime_properties'),
            state=request.json.get('state'),
            version=request.json['version'])
        get_storage_manager().update_node_instance(node)
        return responses.NodeInstance(
            **get_storage_manager().get_node_instance(
                node_instance_id).to_dict())


class DeploymentsIdOutputs(SecuredResource):

    @swagger.operation(
        responseClass=responses.DeploymentOutputs.__name__,
        nickname="get",
        notes="Gets a specific deployment outputs."
    )
    @exceptions_handled
    @marshal_with(responses.DeploymentOutputs.resource_fields)
    def get(self, deployment_id, **_):
        """Get deployment outputs"""
        outputs = get_blueprints_manager().evaluate_deployment_outputs(
            deployment_id)
        return responses.DeploymentOutputs(deployment_id=deployment_id,
                                           outputs=outputs)


def _query_elastic_search(index=None, doc_type=None, body=None):
    """Query ElasticSearch with the provided index and query body.

    Returns:
    Elasticsearch result as is (Python dict).
    """
    es_host = config.instance().db_address
    es_port = config.instance().db_port
    es = elasticsearch.Elasticsearch(hosts=[{"host": es_host,
                                             "port": es_port}])
    return es.search(index=index, doc_type=doc_type, body=body)


class Events(SecuredResource):

    def _query_events(self):
        """
        List events for the provided Elasticsearch query
        """
        verify_json_content_type()
        return _query_elastic_search(index='cloudify_events',
                                     body=request.json)

    @swagger.operation(
        nickname='events',
        notes='Returns a list of events for the provided ElasticSearch query. '
              'The response format is as ElasticSearch response format.',
        parameters=[{'name': 'body',
                     'description': 'ElasticSearch query.',
                     'required': True,
                     'allowMultiple': False,
                     'dataType': 'string',
                     'paramType': 'body'}],
        consumes=['application/json']
    )
    @exceptions_handled
    def get(self):
        """
        List events for the provided Elasticsearch query
        """
        return self._query_events()

    @swagger.operation(
        nickname='events',
        notes='Returns a list of events for the provided ElasticSearch query. '
              'The response format is as ElasticSearch response format.',
        parameters=[{'name': 'body',
                     'description': 'ElasticSearch query.',
                     'required': True,
                     'allowMultiple': False,
                     'dataType': 'string',
                     'paramType': 'body'}],
        consumes=['application/json']
    )
    @exceptions_handled
    def post(self):
        """
        List events for the provided Elasticsearch query
        """
        return self._query_events()


class Search(SecuredResource):

    @swagger.operation(
        nickname='search',
        notes='Returns results from the storage for the provided '
              'ElasticSearch query. The response format is as ElasticSearch '
              'response format.',
        parameters=[{'name': 'body',
                     'description': 'ElasticSearch query.',
                     'required': True,
                     'allowMultiple': False,
                     'dataType': 'string',
                     'paramType': 'body'}],
        consumes=['application/json']
    )
    @exceptions_handled
    def post(self):
        """
        Search using an Elasticsearch query
        """
        verify_json_content_type()
        return _query_elastic_search(index='cloudify_storage',
                                     body=request.json)


class Status(SecuredResource):

    @swagger.operation(
        responseClass=responses.Status,
        nickname="status",
        notes="Returns state of running system services"
    )
    @exceptions_handled
    @marshal_with(responses.Status.resource_fields)
    def get(self):
        """
        Get the status of running system services
        """
        job_list = {'riemann': 'Riemann',
                    'rabbitmq-server': 'RabbitMQ',
                    'celeryd-cloudify-management': 'Celery Management',
                    'elasticsearch': 'Elasticsearch',
                    'cloudify-ui': 'Cloudify UI',
                    'logstash': 'Logstash',
                    'nginx': 'Webserver'
                    }

        try:
            if self._is_docker_env():
                job_list.update({'rest-service': 'Manager Rest-Service',
                                 'amqp-influx': 'AMQP InfluxDB',
                                 })
                from manager_rest.runitsupervise import get_services
                jobs = get_services(job_list)
            else:
                job_list.update({'manager': 'Cloudify Manager',
                                 'rsyslog': 'Syslog',
                                 'ssh': 'SSH',
                                 })
                from manager_rest.upstartdbus import get_jobs
                jobs = get_jobs(job_list.keys(), job_list.values())
        except ImportError:
            jobs = ['undefined']

        return responses.Status(status='running', services=jobs)

    @staticmethod
    def _is_docker_env():
        return os.getenv('DOCKER_ENV') is not None


class ProviderContext(SecuredResource):

    @swagger.operation(
        responseClass=responses.ProviderContext,
        nickname="getContext",
        notes="Get the provider context"
    )
    @exceptions_handled
    @marshal_with(responses.ProviderContext.resource_fields)
    def get(self, _include=None):
        """
        Get provider context
        """
        context = get_storage_manager().get_provider_context(include=_include)
        return responses.ProviderContext(**context.to_dict())

    @swagger.operation(
        responseClass=responses.ProviderContextPostStatus,
        nickname='postContext',
        notes="Post the provider context",
        parameters=[{'name': 'body',
                     'description': 'Provider context',
                     'required': True,
                     'allowMultiple': False,
                     'dataType': requests_schema.PostProviderContextRequest.__name__,  # NOQA
                     'paramType': 'body'}],
        consumes=[
            "application/json"
        ]
    )
    @exceptions_handled
    @marshal_with(responses.ProviderContextPostStatus.resource_fields)
    def post(self):
        """
        Create provider context
        """
        verify_json_content_type()
        request_json = request.json
        verify_parameter_in_request_body('context', request_json)
        verify_parameter_in_request_body('name', request_json)
        context = models.ProviderContext(name=request.json['name'],
                                         context=request.json['context'])
        update = verify_and_convert_bool(
            'update',
            request.args.get('update', 'false')
        )

        status_code = 200 if update else 201

        if update:
            get_storage_manager().update_provider_context(context)
        else:
            get_storage_manager().put_provider_context(context)
        return responses.ProviderContextPostStatus(status='ok'), status_code


class Version(Resource):

    @swagger.operation(
        responseClass=responses.Version,
        nickname="version",
        notes="Returns version information for this rest service"
    )
    @exceptions_handled
    @marshal_with(responses.Version.resource_fields)
    def get(self):
        """
        Get version information
        """
        return responses.Version(**get_version_data())


class EvaluateFunctions(SecuredResource):

    @swagger.operation(
        responseClass=responses.EvaluatedFunctions,
        nickname='evaluateFunctions',
        notes="Evaluate provided payload for intrinsic functions",
        parameters=[{'name': 'body',
                     'description': '',
                     'required': True,
                     'allowMultiple': False,
                     'dataType': requests_schema.EvaluateFunctionsRequest.__name__,  # noqa
                     'paramType': 'body'}],
        consumes=[
            "application/json"
        ]
    )
    @exceptions_handled
    @marshal_with(responses.EvaluatedFunctions.resource_fields)
    def post(self):
        """
        Evaluate intrinsic in payload
        """
        verify_json_content_type()
        request_json = request.json
        verify_parameter_in_request_body('deployment_id', request_json)
        verify_parameter_in_request_body('context', request_json,
                                         optional=True,
                                         param_type=dict)
        verify_parameter_in_request_body('payload', request_json,
                                         param_type=dict)

        deployment_id = request_json['deployment_id']
        context = request_json.get('context', {})
        payload = request_json.get('payload')
        processed_payload = get_blueprints_manager().evaluate_functions(
            deployment_id=deployment_id,
            context=context,
            payload=payload)
        return responses.EvaluatedFunctions(deployment_id=deployment_id,
                                            payload=processed_payload)


class Tokens(SecuredResource):

    @swagger.operation(
        responseClass=responses.Tokens,
        nickname="get auth token for the request user",
        notes="Generate authentication token for the request user",
        )
    @exceptions_handled
    @marshal_with(responses.Tokens.resource_fields)
    def get(self):
        """
        Get authentication token
        """
        if not app.config.get(SECURED_MODE):
            raise manager_exceptions.AppNotSecuredError(
                'token generation not supported, application is not secured')

        if not hasattr(app, 'auth_token_generator'):
            raise manager_exceptions.NoTokenGeneratorError(
                'token generation not supported, an auth token generator was '
                'not registered')

        token = app.auth_token_generator.generate_auth_token()
        return responses.Tokens(value=token)
