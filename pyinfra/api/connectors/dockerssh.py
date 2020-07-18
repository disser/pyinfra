import os

from tempfile import mkstemp

import click
import six

from pyinfra import logger
from pyinfra.api import QuoteString, StringCommand
from pyinfra.api.exceptions import ConnectError, InventoryError, PyinfraError
from pyinfra.api.util import get_file_io, memoize
from pyinfra.progress import progress_spinner

from .ssh import run_shell_command as run_remote_shell_command
from .ssh import connect as ssh_connect
from .util import make_unix_command


@memoize
def show_warning():
    logger.warning('The @dockerssh connector is in beta!')

def make_names_data(host_image_str):
    hostname, image = host_image_str.split(':', 1)
    if not image:
        raise InventoryError('No docker base image provided!')

    show_warning()

    yield '@dockerssh/{0}:{1}'.format(hostname, image), {'ssh_hostname': hostname, 'docker_image': image}, ['@dockerssh']


def connect(state, host):
    client = ssh_connect(state, host)
    # FIXME: kludgy connection setup so we can call run_remote_shell_command()
    host.connection = client
    try:
        with progress_spinner({'docker run'}):
            container_id = run_remote_shell_command(
                state, host,
                'docker run -d {0} tail -f /dev/null'.format(host.data.docker_image),
            )[1][-1]  # last line is the container ID
    except PyinfraError as e:
        raise ConnectError(e.args[0])

    host.host_data['docker_container_id'] = container_id
    return client

def disconnect(state, host):
    container_id = host.host_data['docker_container_id'][:12]

    with progress_spinner({'docker commit'}):
        image_id = run_remote_shell_command(
            state, host,
            'docker commit {0}'.format(container_id),
        )[1][-1][7:19]  # last line is the image ID, get sha256:[XXXXXXXXXX]...

    with progress_spinner({'docker rm'}):
        run_remote_shell_command(
            state, host,
            'docker rm -f {0}'.format(container_id),
        )

    logger.info('{0}docker build complete, image ID: {1}'.format(
        host.print_prefix, click.style(image_id, bold=True),
    ))


def run_shell_command(
    state, host, command,
    get_pty=False,
    timeout=None,
    stdin=None,
    success_exit_codes=None,
    print_output=False,
    print_input=False,
    return_combined_output=False,
    **command_kwargs
):
    container_id = host.host_data['docker_container_id']

    # Don't sudo/su in Docker - is this the right thing to do? Makes deploys that
    # target SSH systems work w/Docker out of the box (ie most docker commands
    # are run as root).
    for key in ('sudo', 'su_user'):
        command_kwargs.pop(key, None)

    command = make_unix_command(command, **command_kwargs)
    command = QuoteString(command)

    docker_flags = '-it' if get_pty else '-i'
    docker_command = StringCommand(
        'docker', 'exec', docker_flags, container_id,
        'sh', '-c', command,
    )

    return run_remote_shell_command(
        state, host, docker_command,
        timeout=timeout,
        stdin=stdin,
        success_exit_codes=success_exit_codes,
        print_output=print_output,
        print_input=print_input,
        return_combined_output=return_combined_output,
    )


def put_file(
    state, host, filename_or_io, remote_filename,
    print_output=False, print_input=False,
    **kwargs  # ignored (sudo/etc)
):
    '''
    Upload a file/IO object to the target Docker container by copying it to a
    temporary location and then uploading it into the container using ``docker cp``.
    '''

    _, temp_filename = mkstemp()

    try:
        # Load our file or IO object and write it to the temporary file
        with get_file_io(filename_or_io) as file_io:
            with open(temp_filename, 'wb') as temp_f:
                data = file_io.read()

                if isinstance(data, six.text_type):
                    data = data.encode()

                temp_f.write(data)

        docker_id = host.host_data['docker_container_id']
        docker_command = 'docker cp {0} {1}:{2}'.format(
            temp_filename,
            docker_id,
            remote_filename,
        )

        status, _, stderr = run_remote_shell_command(
            state, host, docker_command,
            print_output=print_output,
            print_input=print_input,
        )
    finally:
        os.remove(temp_filename)

    if not status:
        raise IOError('\n'.join(stderr))

    if print_output:
        click.echo('{0}file uploaded to container: {1}'.format(
            host.print_prefix, remote_filename,
        ), err=True)

    return status


def get_file(
    state, host, remote_filename, filename_or_io,
    print_output=False, print_input=False,
    **kwargs  # ignored (sudo/etc)
):
    '''
    Download a file from the target Docker container by copying it to a temporary
    location and then reading that into our final file/IO object.
    '''

    _, temp_filename = mkstemp()

    try:
        docker_id = host.host_data['docker_container_id']
        docker_command = 'docker cp {0}:{1} {2}'.format(
            docker_id,
            remote_filename,
            temp_filename,
        )

        status, _, stderr = run_remote_shell_command(
            state, host, docker_command,
            print_output=print_output,
            print_input=print_input,
        )

        # Load the temporary file and write it to our file or IO object
        with open(temp_filename) as temp_f:
            with get_file_io(filename_or_io, 'wb') as file_io:
                data = temp_f.read()

                if isinstance(data, six.text_type):
                    data = data.encode()

                file_io.write(data)
    finally:
        os.remove(temp_filename)

    if not status:
        raise IOError('\n'.join(stderr))

    if print_output:
        click.echo('{0}file downloaded from container: {1}'.format(
            host.print_prefix, remote_filename,
        ), err=True)

    return status


EXECUTION_CONNECTOR = True
