def py_client(**kwargs):
    """A simple Python client for connecting to the gateway.

    For acceptable ``kwargs``, please refer to :cmd:`jina client --help`

    Example, assuming a Flow is "standby" on 192.168.1.100, with port_grpc at 55555.

    .. highlight:: python
    .. code-block:: python

        from jina.clients import py_client

        # to test connectivity
        py_client(port_grpc='192.168.1.100', host=55555).dry_run()

        # to search
        py_client(port_grpc='192.168.1.100', host=55555).search(input_fn, output_fn)

        # to index
        py_client(port_grpc='192.168.1.100', host=55555).index(input_fn, output_fn)
    """
    from ..main.parser import set_client_cli_parser
    from ..helper import get_parsed_args
    from .python import PyClient
    _, args, _ = get_parsed_args(kwargs, set_client_cli_parser(), 'Client')
    return PyClient(args)
