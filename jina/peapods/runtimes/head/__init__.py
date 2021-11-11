import argparse
import asyncio
import multiprocessing
import threading
import time
from abc import ABC
from typing import Optional, Union, List

import grpc
from grpc import RpcError

from ..asyncio import AsyncNewLoopRuntime
from ..request_handlers.data_request_handler import DataRequestHandler
from ...networking import GrpcConnectionPool
from .... import DocumentArray
from ....enums import PollingType
from ....excepts import RuntimeTerminated
from ....proto import jina_pb2_grpc
from ....types.message import Message
from ....types.message.common import ControlMessage


class HeadRuntime(AsyncNewLoopRuntime, ABC):
    """
    Runtime is used in head peas. It responds to Gateway requests and sends to uses_before/uses_after and its workers
    """

    def __init__(
        self,
        args: argparse.Namespace,
        connection_pool: GrpcConnectionPool,
        cancel_event: Optional[
            Union['asyncio.Event', 'multiprocessing.Event', 'threading.Event']
        ] = None,
        **kwargs,
    ):
        """Initialize grpc server for the head runtime.
        :param args: args from CLI
        :param connection_pool: ConnectionPool to use for sending messages
        :param cancel_event: the cancel event used to wait for canceling
        :param kwargs: keyword args
        """
        super().__init__(args, cancel_event, **kwargs)

        self.name = args.name
        self.connection_pool = connection_pool
        self.uses_before_address = args.uses_before_address
        if self.uses_before_address:
            self.connection_pool.add_connection(
                pod='uses_before', address=self.uses_before_address
            )
        self.uses_after_address = args.uses_after_address
        if self.uses_after_address:
            self.connection_pool.add_connection(
                pod='uses_after', address=self.uses_after_address
            )
        self.polling = args.polling if hasattr(args, 'polling') else PollingType.ANY

    async def async_setup(self):
        """ Wait for the GRPC server to start """
        self._grpc_server = grpc.aio.server(
            options=[
                ('grpc.max_send_message_length', -1),
                ('grpc.max_receive_message_length', -1),
            ]
        )

        jina_pb2_grpc.add_JinaDataRequestRPCServicer_to_server(self, self._grpc_server)
        self._grpc_server.add_insecure_port(f'0.0.0.0:{self.args.port_in}')
        await self._grpc_server.start()

    async def async_run_forever(self):
        """Block until the GRPC server is terminated """
        await self._grpc_server.wait_for_termination()

    async def async_cancel(self):
        """Stop the GRPC server"""
        self.logger.debug('Cancel HeadRuntime')

        await self._grpc_server.stop(0)

    @staticmethod
    def is_ready(ctrl_address: str, **kwargs) -> bool:
        """
        Check if status is ready.

        :param ctrl_address: the address where the control message needs to be sent
        :param kwargs: extra keyword arguments

        :return: True if status is ready else False.
        """

        try:
            GrpcConnectionPool.send_message_sync(ControlMessage('STATUS'), ctrl_address)
        except RpcError:
            return False

        return True

    @staticmethod
    def wait_for_ready_or_shutdown(
        timeout: Optional[float],
        shutdown_event: Union[multiprocessing.Event, threading.Event],
        ctrl_address: str,
        **kwargs,
    ):
        """
        Check if the runtime has successfully started

        :param timeout: The time to wait before readiness or failure is determined
        :param shutdown_event: the multiprocessing event to detect if the process failed
        :param ctrl_address: the address where the control message needs to be sent
        :param kwargs: extra keyword arguments
        :return: True if is ready or it needs to be shutdown
        """
        timeout_ns = 1000000000 * timeout if timeout else None
        now = time.time_ns()
        while timeout_ns is None or time.time_ns() - now < timeout_ns:
            if shutdown_event.is_set() or HeadRuntime.is_ready(ctrl_address):
                return True
            time.sleep(0.1)

        return False

    async def _handle_messages(self, messages: List[Message]) -> Message:
        # we assume that all messages have the same type, so we need to check only the first
        if messages[0].envelope.request_type != 'DataRequest':
            # ControlRequest messages need to be processed one by one
            # Responses should not matter, so just the last message is returned
            last_response = None
            for message in messages:
                last_response = await self._handle_control_request(message)
            return last_response
        else:
            return await self._handle_data_requests(messages)

    async def Call(self, messages: List[Message], *args) -> Message:
        """Process they received messages and return the result as a new message

        :param messages: the messages to process
        :param args: additional arguments in the grpc call, ignored
        :returns: the response message
        """
        try:
            return await self._handle_messages(messages)
        except RuntimeTerminated:
            HeadRuntime.cancel(self.is_cancel)
        except (RuntimeError, Exception) as ex:
            self.logger.error(
                f'{ex!r}' + f'\n add "--quiet-error" to suppress the exception details'
                if not self.args.quiet_error
                else '',
                exc_info=not self.args.quiet_error,
            )
            raise

    async def _handle_control_request(self, msg: Message) -> Message:
        if self.logger.debug_enabled:
            self._log_info_msg(msg)

        if msg.request.command == 'TERMINATE':
            raise RuntimeTerminated()
        elif msg.request.command == 'ACTIVATE':

            for relatedEntity in msg.request.relatedEntities:
                connection_string = f'{relatedEntity.address}:{relatedEntity.port}'

                self.connection_pool.add_connection(
                    pod='worker',
                    address=connection_string,
                    shard_id=relatedEntity.shard_id
                    if relatedEntity.HasField('shard_id')
                    else None,
                )
        elif msg.request.command == 'DEACTIVATE':
            for relatedEntity in msg.request.relatedEntities:
                connection_string = f'{relatedEntity.address}:{relatedEntity.port}'
                self.connection_pool.remove_connection(
                    pod='worker',
                    address=connection_string,
                    shard_id=relatedEntity.shard_id,
                )
        return msg

    async def _handle_data_requests(self, messages: List[Message]) -> Message:
        if self.logger.debug_enabled:
            self._log_info_messages(messages)

        if self.uses_before_address:
            messages = [
                await self.connection_pool.send_messages_once(
                    messages, pod='uses_before'
                )
            ]

        worker_send_tasks = self.connection_pool.send_messages(
            messages=messages, pod='worker', polling_type=self.polling
        )
        worker_results = [
            await result for result in asyncio.as_completed(worker_send_tasks)
        ]

        # If there is no uses_after, the head needs to concatenate the documents returned from the workers
        if self.uses_after_address:
            response_message = await self.connection_pool.send_messages_once(
                worker_results, pod='uses_after'
            )
        elif len(worker_results) > 1:
            # TODO: this logic should not be here, also this should change once List[Message] is replaced with requests in proto
            partial_requests = [message.request for message in worker_results]
            result = DocumentArray(
                [d for r in reversed(partial_requests) for d in getattr(r, 'docs')]
            )
            # the docs needs to be stored in the message returned to the caller, artifically choose the first one here
            response_message = worker_results[0]
            DataRequestHandler.replace_docs(response_message, result)
        elif len(worker_results) == 1:
            # there are not multiple messages as input, just return the single one in the list
            response_message = worker_results[0]
        else:
            raise RuntimeError(
                f'Head {self.name} did not receive a response when sending message to worker peas'
            )

        return response_message