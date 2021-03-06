########
# Copyright (c) 2013 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#    * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    * See the License for the specific language governing permissions and
#    * limitations under the License.


from setuptools import setup


setup(
    name='cloudify-rest-service',
    version='3.2rc1',
    author='Dan Kilman',
    author_email='dank@gigaspaces.com',
    packages=['manager_rest'],
    package_data={'manager_rest': ['VERSION']},
    license='LICENSE',
    description='Cloudify manager rest service',
    zip_safe=False,
    install_requires=[
        'six==1.8.0',
        'Flask==0.10.1',
        'flask-restful==0.2.5',
        'flask-restful-swagger==0.12',
        'supervise==1.1.1',
        'cloudify-dsl-parser==3.2rc1',
        'requests==2.2.1',
        'gunicorn==18.0',
        'PyYAML==3.10',
        'elasticsearch==1.0.0',
        'celery==3.1.17',
        'flask-securest==0.6',
    ]
)
