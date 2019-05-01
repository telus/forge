#!/usr/bin/env python3

# Copyright (c) 2015 Chris Olstrom <chris@olstrom.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import os
import re
import argparse
import httplib2
import subprocess
import json

def install_with_pip(packages):
    """ Installs packages with pip """
    for package in packages:
        subprocess.call('sudo -H python3 -m pip install -U ' + package, shell=True)


def detect(setting):
    """ Detects a setting in tags, falls back to environment variables """
    if setting in resource_tags():
        return resource_tags()[setting]
    else:
        return os.getenv(shell_style(setting))


def shell_style(name):
    """ Translates reasonable names into names you would expect for environment
    variables. Example: 'ForgeRegion' becomes 'FORGE_REGION' """
    return re.sub('(?!^)([A-Z]+)', r'_\1', name).upper()


def download_from_s3(source, destination):
    """ Downloads a file from an S3 bucket """
    subprocess.call("aws s3 cp --region {region} s3://{bucket}/{file} {save_to}".format(
        region=detect('ForgeRegion'),
        bucket=detect('ForgeBucket'),
        file=source,
        save_to=destination
    ), shell=True)


def download_directory_from_s3(source, destination):
    """ Downloads a directory from an S3 bucket """
    source = 's3://' + detect('ForgeBucket') + '/' + source
    subprocess.call(['aws', 's3', 'cp', '--recursive', '--region', detect('ForgeRegion'), source, destination])


def instance_metadata(item):
    """ Returns information about the current instance from EC2 Instace API """
    h = httplib2.Http(".cache")
    resp, content = h.request("http://169.254.169.254/latest/meta-data/{}".format(item), "GET")
    return content

def instance_id():
    """ Returns the ID of the current instance """
    return instance_metadata('instance-id')


def region():
    """ Returns the region the current instance is located in """
    return instance_metadata('placement/availability-zone')[:-1].decode('utf-8')


def resource_tags():
    """ Returns a dictionary of all resource tags for the current instance """
    result_bytes = subprocess.check_output(
        "aws ec2 describe-tags --region {region} --filters \"Name=resource-id,Values={instance_id}\"".format(
            region=region(),
            instance_id=(str(instance_id(), "UTF-8"))
        ), shell=True)

    aws_tags = json.loads(result_bytes.decode("UTF-8")).get("Tags")
    resource_tags_dict = {}
    for entry in aws_tags:
      resource_tags_dict[entry.get("Key")] = entry.get("Value")

    return resource_tags_dict


def security_groups():
    """ Returns a list of sercurity groups for the current instance """
    return instance_metadata('security-groups').split('\n')


def infer_tags(security_group):
    """ Attempts to infer tags from a security group name """
    matches = re.search(r'(?P<Project>[\w-]+)-(?P<Role>\w+)$', security_group)
    return matches.groupdict()


def implicit_tags():
    """ Returns a list of tags inferred from security groups """
    return [infer_tags(name) for name in security_groups()]


def discover(trait):
    """ Tries to find a trait in tags, makes a reasonable guess if it fails """
    if trait in resource_tags():
        return [resource_tags()[trait]]
    else:
        return [implicit_tags()[trait]]


def project_path():
    """ Returns the forge path for the discovered project """
    return discover('Project')[0] + '/'


def role_paths():
    """ Returns a list of forge paths for all discovered roles """
    return [project_path() + role + '/' for role in discover('Role')]


def unique(enumerable):
    """ Returns a list without duplicate items """
    return list(set(enumerable))


def applicable_playbooks():
    """ Returns a list of playbooks that should be applied to this system """
    playbooks = []

    # Base Playbook
    if not args.skip_base_playbook:
      playbooks = ['']

    # Project Playbook
    if not args.skip_project_playbook:
      playbooks.append(project_path())

    # System Roles Playbook
    if not args.skip_role_playbook:
      playbooks.extend(role_paths())

    return sorted(unique(playbooks), key=len)


def flat_path(path):
    """ Flattens a path by substituting dashes for slashes """
    import re
    return re.sub('/', '-', path)

def playbook_directory(playbook):
    """ construct a directory from playbook """
    import os
    if len(playbook) == 0:
        directory = 'base'
    else:
        directory = flat_path(playbook.strip('/'))
    directory = os.path.join(os.sep, 'tmp', directory)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    return os.path.join(directory, '') # returns with tailing slash


def get_dependencies(playbook):
    """ Downloads and installs all roles required for a playbook to run """
    path = playbook_directory(playbook)
    if not args.skip_download:
        download_from_s3(playbook + 'dependencies.yml', path + 'dependencies.yml')
    subprocess.call('ansible-galaxy install -ifr' + path + 'dependencies.yml', shell=True)


def get_vault(playbook):
    """ Downloads a vault file, and puts it where Ansible can find it. """
    vault_name = flat_path(playbook)[:-1]
    if len(vault_name) == 0:
        vault_name = 'all'
    vault_file = '/etc/ansible/group_vars/' + vault_name + '.yml'
    if not args.skip_download:
        download_from_s3(playbook + 'vault.yml', vault_file)
    with open('/etc/ansible/hosts', 'a') as stream:
        stream.writelines(["\n[" + vault_name + "]\n", 'localhost\n'])

def get_templates(playbook):
    """ Downloads playbook templates """
    import os
    import shutil
    path = playbook_directory(playbook) + 'templates'
    if not args.skip_download:
        if os.path.isdir(path):
            shutil.rmtree(path)
        download_directory_from_s3(playbook + 'templates', path)


def configure_environment():
    """ Exposes information from Resource Tags in Ansible vars """
    get_vault('')
    with open('/etc/ansible/group_vars/local.yml', 'w+') as stream:
        stream.write("\nproject: " + resource_tags()['Project'])
        stream.write("\nenvironment_tier: " + resource_tags()['Environment'])
        stream.write("\nsystem_role: " + resource_tags()['Role'])


def record_exit(playbook, exit_status):
    """ Saves exit status of playbook for notfication purposes"""
    playbook_name = playbook_directory(playbook) + 'playbook' + '.status'
    with open(playbook_name, 'w+') as stream:
        stream.write(str(exit_status))


def execute(playbook):
    """ Downloads and executes a playbook. """
    path = playbook_directory(playbook)
    for hook in ['pre-', '', 'post-']:
        filename = hook + 'playbook.yml'
        if not args.skip_download:
            download_from_s3(playbook + filename, path + filename)
        # Avoid 'file not found' messages from ansible-playbook
        import os.path
        if os.path.isfile(path + filename):
            exit_status = subprocess.call('ansible-playbook ' + path + filename, shell=True)
            record_exit(playbook, exit_status)
        else:
            print('%s file not found, so not executed with ansible-playbook' % (path + filename))


def ssh_keyscan(host):
    """ Get the SSH host key from a remote server by connecting to it """
    import paramiko
    with paramiko.transport.Transport(host) as ssh:
        ssh.start_client()
        return ssh.get_remote_server_key()


def ssh_host_key(host, port=22):
    """ Get SSH host key, return string formatted for known_hosts """
    if port != 22:
        host = "{host}:{port}".format(host=host, port=port)
    key = ssh_keyscan(host)
    return "{host} {key_name} {key}".format(
        host=host,
        key_name=key.get_name(),
        key=key.get_base64())


def in_known_hosts(host_key):
    """ Checks if a key is in known_hosts """
    if not os.path.isfile('/etc/ssh/ssh_known_hosts'):
        return False
    with open('/etc/ssh/ssh_known_hosts', 'r') as known_hosts:
        for entry in known_hosts:
            if host_key in entry:
                return True
    return False


def add_to_known_hosts(host_key):
    """ Appends line to a file """
    if in_known_hosts(host_key):
        return
    with open('/etc/ssh/ssh_known_hosts', 'a') as known_hosts:
        known_hosts.write(host_key + "\n")


def configure_ansible():
    """ Fetches ansible configurations from ForgeBucket """
    download_from_s3('ansible.hosts', '/etc/ansible/hosts')
    download_from_s3('ansible.cfg', '/etc/ansible/ansible.cfg')
    download_from_s3('vault.key', '/etc/ansible/vault.key')
    files = ['/etc/ansible/ansible.cfg', '/etc/ansible/vault.key']
    set_permissions(files, 0o400)
    add_to_known_hosts(ssh_host_key('github.com'))
    add_to_known_hosts(ssh_host_key('bitbucket.org'))


def set_permissions(files, mode):
    """ Sets permissions on a list of files """
    for filename in files:
        try:
            os.chmod(filename, mode)
        except OSError:
            pass


def get_credentials():
    """ Fetches credentials needed for private repositories """
    download_from_s3('ssh.ed25519', '/root/.ssh/id_ed25519')
    download_from_s3('ssh.rsa', '/root/.ssh/id_rsa')
    set_permissions(['/root/.ssh/id_ed25519', '/root/.ssh/id_rsa'], 0o400)


def preconfigure():
    """ Configure everything needed to configure everything else. """
    if args.skip_preconfigure:
      return
    install_with_pip(['ansible==2.2.0.0'])
    configure_ansible()
    configure_environment()
    get_credentials()
    download_from_s3('bin/reforge', '/usr/local/sbin/reforge')
    set_permissions(['/usr/local/sbin/reforge'], 0o500)


def self_provision():
    """ Bring it all together and follow your dreams, little server! """
    preconfigure()
    for playbook in applicable_playbooks():
        get_dependencies(playbook)
        get_vault(playbook)
        get_templates(playbook)
        execute(playbook)

parser = argparse.ArgumentParser()
parser.add_argument('--skip-preconfigure', action='store_true', help='Skip pre-configuration')
parser.add_argument('--skip-base-playbook', action='store_true', help='Skip base playbook')
parser.add_argument('--skip-project-playbook', action='store_true', help='Skip project playbook')
parser.add_argument('--skip-role-playbook', action='store_true', help='Skip role playbook')
parser.add_argument('--skip-download', action='store_true', help='Skip download, so you can test the playbooks in /tmp')
args = parser.parse_args()

self_provision()

