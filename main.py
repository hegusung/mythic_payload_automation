#!/usr/bin/python3
import argparse
import asyncio
from mythic import mythic, mythic_utilities, mythic_classes, graphql_queries
from typing import AsyncGenerator, List, Union
import os
import yaml
import gql
import json
import time
from datetime import datetime
import hashlib

import warnings
warnings.filterwarnings("ignore", message="WARNING: By default, AIOHTTPTransport does not verify ssl certificates")
warnings.filterwarnings("ignore", category=UserWarning)


def green(text):
    print(f"\033[92m{text}\033[0m")

def blue(text):
    print(f"\033[94m{text}\033[0m")

def red(text):
    print(f"\033[91m{text}\033[0m")


def sha1_hex(buf: bytes) -> str:
    """Return SHA-1 hex digest of a bytes-like buffer."""
    h = hashlib.sha1()
    h.update(buf)                 # buf can be bytes/bytearray/memoryview
    return h.hexdigest()

async def upload_new_file(mythic_instance, filename, filecontents):

    sha1_hash = sha1_hex(filecontents)

    todelete = []
    blue("[*] Searching files with the same hash... (%s: %s)" % (filename, sha1_hash))
    async for batch in get_all_payload_files(mythic=mythic_instance):
        for u in batch:
            if u['deleted'] == True or u['complete'] == False:
                continue

            if u['sha1'] == sha1_hash:
                blue("[*] Found file (%s: %s)" % (u['filename_utf8'], u['id']))
                return u['agent_file_id']

    blue("[*] Creating file %s" % filename)
    # create file
    resp = await mythic.register_file(
        mythic=mythic_instance, filename=filename, contents=filecontents
    )
    green("[+] Successfully created file in mythic: %s" % resp)

    return resp

async def get_payload_uuid(mythic_instance, payload_name):
    payload_uuid = None
    payload_creation = None


    payload_list = await mythic.get_all_payloads(mythic=mythic_instance)
    for payload in payload_list:
        if payload['deleted'] == True or payload['build_phase'] != "success":
            continue

        if payload_name == payload['filemetum']['filename_utf8']:
            item_creation = datetime.strptime(payload['creation_time'], "%Y-%m-%dT%H:%M:%S.%f")
            if payload_uuid == None or payload_creation < item_creation:
                payload_uuid = {'uuid': payload['uuid'], 'file_uuid': payload['filemetum']['agent_file_id']}
                payload_creation = item_creation

    return payload_uuid

async def create_stage(mythic_instance, payload, c2_url):

    # Prepare build parameters
    build_parameters = []
    for key, value in payload['parameters'].items():
        if type(value) == str and value.startswith("file:") and not value.startswith("file://"):
            param_file = os.path.join("files", value[len("file:"):])
            
            with open(param_file, "rb") as f2:
                file_content = f2.read()

                value = await upload_new_file(mythic_instance, value[len("file:"):], file_content)

        elif type(value) == str and value.startswith("payload:"):
            value = await get_payload_uuid(mythic_instance, value[len("payload:"):])
            value = value['file_uuid']

            if value == None:
                raise Exception("Failed to get the following payload: %s" %  value[len("payload:"):])

        elif value == None:
            value = ""

        build_parameters.append({
            'name': key,
            'value': value,
        })

    if 'wrapper' in payload and payload['wrapper'] == True:
        wrapper = True
        downloader = False
        if len(payload['wrapped_payload']) != 0:
            wrapped_payload = await get_payload_uuid(mythic_instance, payload['wrapped_payload'])

            if wrapped_payload == None:
                raise Exception("Payload %s doesn't exist in Mythic !" % payload['wrapped_payload'])

            wrapped_payload = wrapped_payload['uuid']

        else:
            wrapped_payload = ""
    elif 'downloader' in payload and payload['downloader'] == True:
        wrapper = False
        downloader = True
        downloaded_payload = await get_payload_uuid(mythic_instance, payload['downloaded_payload'])

        if downloaded_payload == None:
            raise Exception("Payload %s doesn't exist in Mythic !" % payload['downloaded_payload'])

        registered_file_uuid = downloaded_payload['file_uuid']
        payload_uuid = downloaded_payload['uuid']

        # Get C2 ID
        c2_profiles_info = await mythic_utilities.graphql_post(
            mythic=mythic_instance,
            gql_query=gql.gql("query getC2Profiles {  c2profile(    where: {deleted: {_eq: false}, container_running: {_eq: true}, is_p2p: {_eq: false}}  ) {    id    name    __typename  }}"),
        )

        c2_profile_id = None
        for profile in c2_profiles_info['c2profile']:
            if profile['name'] == payload['c2_profile']:
                c2_profile_id = profile['id']
                break

        if not c2_profile_id:
            raise Exception("C2 profile not found")

        # Create a C2 URL to the payload
        res = await mythic_utilities.graphql_post(
            mythic=mythic_instance,
            gql_query=gql.gql("mutation hostFileMutation($c2_id: Int!, $file_uuid: String!, $host_url: String!, $alert_on_download: Boolean, $remove: Boolean) {\n  c2HostFile(\n    c2_id: $c2_id\n    file_uuid: $file_uuid\n    host_url: $host_url\n    alert_on_download: $alert_on_download\n    remove: $remove\n  ) {\n    status\n    error\n    __typename\n  }\n}"),
            variables={"c2_id":c2_profile_id, "file_uuid": registered_file_uuid, "host_url": payload['profile_url'], "alert_on_download": False, "remove": False}
        )
        if res['c2HostFile']['status'] != 'success':
            raise Exception("Failed to expose payload: %s" % res)

        url = "%s%s" % (c2_url[payload['c2_profile']], payload['profile_url'])

        # Add config parameter
        build_parameters.append({
            'name': payload['url_parameter'],
            'value': url,
        })
        
    else:
        wrapper = False
        downloader = False
        wrapped_payload = ""

        c2_profiles = []

        for profile in payload['c2_profiles']:

            for key, value in profile['c2_profile_parameters'].items():
                if type(value) == str and value.startswith("file:"):
                    param_file = os.path.join("files", value[len("file:"):])
                    
                    with open(param_file, "rb") as f2:
                        file_content = f2.read()

                        value = await upload_new_file(mythic_instance, value[len("file:"):], file_content)

                        profile['c2_profile_parameters'][key] = value

            c2_profiles.append(profile)

        commands = payload['commands']

    blue("[*] Creating payload %s [%s]" % (payload['name'], payload['payload']))
    try:
        if not wrapper and not downloader:
            payload_response = await mythic.create_payload(
                    mythic=mythic_instance,
                    payload_type_name=payload['payload'],
                    filename=payload['name'],
                    operating_system=payload['os'],
                    commands=commands,
                    c2_profiles=c2_profiles,
                    build_parameters=build_parameters,
                    return_on_complete=True,
            )
        elif downloader and not wrapper:
            payload_response = await mythic.create_wrapper_payload(
                    mythic=mythic_instance,
                    payload_type_name=payload['payload'],
                    filename=payload['name'],
                    operating_system=payload['os'],
                    wrapped_payload_uuid=payload_uuid,
                    build_parameters=build_parameters,
                    return_on_complete=True,
            )
        elif wrapper and not downloader:
            payload_response = await mythic.create_wrapper_payload(
                    mythic=mythic_instance,
                    payload_type_name=payload['payload'],
                    filename=payload['name'],
                    operating_system=payload['os'],
                    wrapped_payload_uuid=wrapped_payload,
                    build_parameters=build_parameters,
                    return_on_complete=True,
            )

        green("[+] Payload %s created: %s" % (payload['name'], payload_response['build_message']))

    except gql.transport.exceptions.TransportQueryError as e:
        red("[-] Error building %s: %s" % (payload['name'], str(e)))


async def delete_old(mythic_instance, payload):
    payload_list = await mythic.get_all_payloads(mythic=mythic_instance)
    for old in payload_list:
        if old['deleted'] == True:
            continue

        if old['filemetum']['filename_utf8'] == payload['name']:
            blue("[*] Deleting old payload: %s [%s]" % (old['filemetum']['filename_utf8'], old['uuid']))

            res = await mythic_utilities.graphql_post(
                mythic=mythic_instance,
                gql_query=gql.gql("mutation PayloadsDeletePayloadMutation($payload_uuid: String!) {  updatePayload(payload_uuid: $payload_uuid, deleted: true) {    status    error    id    deleted    __typename  }}"),
                variables={"payload_uuid": old['uuid']},
            )

            if res['updatePayload']['status'] == 'success':
                green("[+] Successfully deleted payload %s" % (old['uuid'],))
            else:
                red("[-] Failed to delete payload %s: %s" % (old['uuid'], res['updatePayload']['error']))

def yml_generator(config_path):

    if os.path.isfile(config_path):
        f = open(config_path, "r")
        yml_data = yaml.safe_load(f)
        f.close()

        yield yml_data

    elif os.path.isdir(config_path):
        for filename in  sorted(f for f in os.listdir(config_path) if os.path.isfile(os.path.join(config_path, f))):
            full_path = os.path.join(config_path, filename)

            f = open(full_path, "r")
            yml_data = yaml.safe_load(f)
            f.close()

            yield yml_data



async def get_all_payload_files(
        mythic: mythic_classes.Mythic, custom_return_attributes: str = None, batch_size: int = 10,
) -> AsyncGenerator:
    file_query = f"""
    query uploadedFiles($batch_size: Int!, $offset: Int!){{
        filemeta(where: {{deleted: {{_eq: false}}, is_screenshot: {{_eq: false}}, is_download_from_agent: {{_eq: false}}, eventgroup_id: {{_is_null: true}}, is_payload: {{_eq: false}}}}, order_by: {{id: asc}}, limit: $batch_size, offset: $offset){{
            {custom_return_attributes if custom_return_attributes is not None else '...file_data_fragment'}
        }}
    }}
    {graphql_queries.file_data_fragment if custom_return_attributes is None else ''}
    """
    offset = 0
    while True:
        output = await mythic_utilities.graphql_post(
            mythic=mythic, query=file_query, variables={"batch_size": batch_size, "offset": offset}
        )
        if len(output["filemeta"]) > 0:
            yield output["filemeta"]
            offset += len(output["filemeta"])
        else:
            break


async def main(config_path, cleanup):

    f = open("mythic.yml", "r")
    mythic_config = yaml.safe_load(f)
    f.close()

    mythic_instance = await mythic.login(
        username=mythic_config["mythic_username"],
        password=mythic_config["mythic_password"],
        server_ip=mythic_config["mythic_host"],
        server_port=mythic_config["mythic_port"],
        timeout=-1
    )

    for config in yml_generator(config_path):
        for payload in config['payloads']:
            # Delete old payload if already exists
            await delete_old(mythic_instance, payload)

            if not cleanup:
                await create_stage(mythic_instance, payload, mythic_config["mythic_c2_url"])

            time.sleep(0.1)

    for task in asyncio.all_tasks():
        if task is not asyncio.current_task():
            task.cancel()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='MythicAutomation')
    parser.add_argument('--config', metavar='config file of folder', type=str, nargs='?', help='Config file or folder', dest='config')
    parser.add_argument("--cleanup", action='store_true', help='Delete payloads and files', dest='cleanup')

    args = parser.parse_args()

    if args.config:
        asyncio.run(main(args.config, args.cleanup))

