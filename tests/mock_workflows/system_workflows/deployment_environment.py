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

__author__ = 'ran'

from cloudify.decorators import workflow


@workflow
def create(ctx, **kwargs):
    # taken from original deployment_environment create workflow
    ctx.execute_task('riemann_controller.tasks.create',
                     kwargs=kwargs.get('policy_configuration', {}))


@workflow
def delete(ctx, **kwargs):
    # taken from original deployment_environment delete workflow
    ctx.execute_task('riemann_controller.tasks.delete')