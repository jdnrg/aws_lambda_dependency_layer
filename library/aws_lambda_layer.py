#!/usr/bin/python3
# -*- coding: utf-8 -*-

# Copyright: (c) 2019, Diógenes Oliveira <diogenes1oliveira@gmail.com>
# The MIT License (see LICENSE or https://opensource.org/licenses/MIT)

from __future__ import print_function

from base64 import b64encode
import hashlib
import logging
import os
from shutil import rmtree
from tempfile import mkdtemp
import traceback
from zipfile import ZipFile

from ansible.module_utils.basic import AnsibleModule
import boto3

LOGGER = logging.getLogger(__file__)
LOGGER.setLevel(os.getenv('LOGGING_LEVEL') or logging.INFO)


ANSIBLE_METADATA = {
    'metadata_version': '1.1',
    'status': ['preview'],
    'supported_by': 'community',
}

DOCUMENTATION = '''
---
module: aws_lambda_layer

short_description: Manage AWS Lambda Layers

version_added: "2.4"

description:
    - Deploy and publish versions of AWS Lambda Layers

options:
    name:
        description:
            - Name of the layer to be created
        required: True
    state:
        description:
            - Whether to create or destroy the Lambda Layer
        default: 'present'
        choices: ['present', 'absent']
    path:
        description:
            - Path to the ZIP file in the filesystem
            - Required if state == 'present'
        default: None
    bucket:
        description:
            - Bucket where the layer bundle is stored
        required: True
    object_key:
        description:
            - Key to the layer bundle in the bucket
        required: True
    object_version:
        description:
            - Version of the object
        default: None

author:
    - Diógenes Oliveira (@diogenes1oliveira)
'''

EXAMPLES = '''
- name: Upload a ZIP and publish the layer
  aws_lambda_layer:
    name: my-layer
    path: /home/me/layer.zip
    bucket: generic-s3-bucket
    object: bundle/my-layer.zip
'''

RETURN = '''
name:
    description: name of the layer
    type: str
object_version:
    description: version ID of the archive in S3
    type: str
bucket:
    description: Bucket where the layer bundle is stored
    type: str
arn:
    description: ARN of the layer
    returned: success
    type: str
version:
    description: number of the published version of this layer
    returned: success
    type: int
version_arn:
    description: ARN of the published version
    returned: success
    type: str
version_checksum:
    description: SHA-256 of the published bundle
    returned: success
    type: str
stderr:
    description: error message
    returned: failure
    type: str
'''


def get_file_checksum(path):
    """
    Calculates the checksum of the local file.
    """
    with open(path, 'rb') as fp:
        hasher = hashlib.sha256()
        chunk = fp.read(1_000_000)
        while chunk:
            hasher.update(chunk)
            chunk = fp.read(1_000_000)
        return b64encode(hasher.digest()).decode('ascii')


def fetch_s3_checksum(bucket, object_key, object_version=None, metadata='sha256'):
    """
    Gets the SHA-256 checksum of the object via the metadata
    'sha256' or downloading and calculating locally if that is not available.

    Returns a tuple (checksum, downloaded).
    """
    s3_client = boto3.client('s3')

    try:
        options = dict(
            Bucket=bucket,
            Key=object_key,
        )
        if object_version:
            options['VersionId'] = object_version
        obj = s3_client.get_object(**options)
        if not obj['Metadata'].get(metadata, None):
            LOGGER.info(
                'No metadata %r, downloading to calculate the checksum', metadata)
            hasher = hashlib.sha256()
            hasher.update(obj['Body'].read())
            return b64encode(hasher.digest()).decode('ascii'), True
        else:
            return obj['Metadata'][metadata], False

    except s3_client.exceptions.NoSuchKey:
        return None, None


def upload_file(path, bucket, object_key, checksum=None, metadata='sha256'):
    """
    Uploads the file to S3.

    Returns the version ID or None
    """
    s3_client = boto3.client('s3')

    checksum = checksum or get_file_checksum(path)
    with open(path, 'rb') as fp:
        obj = s3_client.put_object(
            Body=fp,
            Bucket=bucket,
            Key=object_key,
            Metadata={
                metadata: checksum,
            },
        )
        return obj.get('VersionId', '') or None


def get_layer_version_info(name, lambda_client=None):
    """
    Returns info (via lambda_client.get_layer_version) of the last layer version
    with the given name.
    """
    lambda_client = lambda_client or boto3.client('lambda')
    versions = set()

    response = lambda_client.list_layer_versions(LayerName=name)
    versions |= {v['Version'] for v in response['LayerVersions']}
    marker = response.get('NextMarker', None)

    while marker:
        response = lambda_client.list_layer_versions(
            LayerName=name, NextMarker=marker)
        versions |= {v['Version'] for v in response['LayerVersions']}
        marker = response.get('NextMarker', None)

    try:
        last_version = max(versions)
    except ValueError:
        return None
    else:
        return lambda_client.get_layer_version(
            LayerName=name,
            VersionNumber=last_version,
        )


def manage_lambda_layer(name, bucket, object_key, object_version, path, state, metadata='sha256'):
    result = dict(
        changed=False,
    )
    lambda_client = boto3.client('lambda')
    s3_checksum, downloaded = fetch_s3_checksum(
        bucket, object_key, object_version, metadata=metadata)
    local_checksum = get_file_checksum(path)

    result['bucket'] = bucket
    result['object_version'] = object_version
    result['downloaded'] = downloaded

    if not s3_checksum or local_checksum != s3_checksum:
        object_version = upload_file(
            path, bucket, object_key, local_checksum, metadata=metadata)
        result['changed'] = True

    layer = get_layer_version_info(name, lambda_client)
    LOGGER.info('bool(get_layer_version_info): %s', bool(layer))
    if layer:
        result['arn'] = layer['LayerArn']
        result['version'] = layer['Version']
        result['version_arn'] = layer['LayerVersionArn']
        result['version_checksum'] = layer['Content']['CodeSha256']

    if not layer or result['version_checksum'] != local_checksum:
        options = {
            'S3Bucket': bucket,
            'S3Key': object_key,
        }
        if object_version:
            options['S3ObjectVersion'] = object_version

        layer = lambda_client.publish_layer_version(
            LayerName=name,
            Content=options,
        )
        result['changed'] = True
        result['arn'] = layer['LayerArn']
        result['version'] = layer['Version']
        result['version_arn'] = layer['LayerVersionArn']
        result['version_checksum'] = layer['Content']['CodeSha256']

    return result


def run_module():
    # define available arguments/parameters a user can pass to the module
    module_args = dict(
        name=dict(type='str', required=True),
        state=dict(type='str', default='present',
                   choices=['present', 'absent']),
        path=dict(type='str', default=None),
        bucket=dict(type='str', required=True),
        object_key=dict(type='str', required=True),
        object_version=dict(type='str', default=None),
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=False,
    )

    result = dict(
        changed=False,
        failed=False,
        name=module.params['name'],
    )

    if module.check_mode:
        return result

    try:
        result.update(
            manage_lambda_layer(
                name=module.params['name'],
                state=module.params['state'],
                bucket=module.params['bucket'],
                object_key=module.params['object'],
                object_version=module.params['object_version'] or None,
                path=module.params['path'],
            )
        )
    except Exception as e:
        LOGGER.exception('Failure')
        result['failed'] = True
        result['stderr'] = traceback.format_exc()
        module.fail_json(msg=str(e), **result)
    else:
        module.exit_json(**result)


def main():
    run_module()


if __name__ == '__main__':
    main()