#!/usr/bin/env python3

"""
Tornado-based server with REST API support
"""

from abc import ABC
from json import dumps
from logging import DEBUG
from typing import Any

import tornado.log
from tornado import httputil
from tornado.escape import json_decode, json_encode
from tornado.ioloop import IOLoop
from tornado.options import parse_command_line
from tornado.web import RequestHandler, Application

from src.db.crud import TaskCrud
from src.db.database import Engine, Base
from src.queue.publisher import Publisher
from src.server.handlers.task_spawner import TaskSpawner
from src.server.structures.response import ServerResponse
from src.server.structures.task import TaskItem
from src.server.structures.task import TaskStatus
from src.cache.redis import RedisCache

# Set logging level for Tornado Server
tornado.log.access_log.setLevel(DEBUG)

# Define our logger
logger = tornado.log.app_log

# Initialize publisher
publisher = Publisher()

# Initialize redis
redis = RedisCache()


class BaseHandler(RequestHandler, ABC):
    """
    Add basic handler to support 'success', 'error'
    messages and more
    """

    def __init__(
        self,
        application: "Application",
        request: httputil.HTTPServerRequest,
        **kwargs: Any,
    ):
        super().__init__(application, request, **kwargs)
        self.server_response = ServerResponse()

    def set_default_headers(self) -> None:
        """
        Set default header to 'application/json' because of
        the endpoint nature (JSON-based response)
        :return: None
        """
        self.set_header("Content-Type", "application/json")

    def success(self, msg: str = "") -> None:
        """
        Return success status
        :param msg: additional message
        :return: None
        """
        self.set_status(status_code=200)
        response = self.server_response.success(msg=msg)
        self.write(response)

    def error(self, msg: str = "") -> None:
        """
        Return error status
        :param msg: additional message
        :return: None
        """
        self.set_status(status_code=500)
        response = self.server_response.error(msg=msg)
        self.write(response)


class CreateTaskHandler(BaseHandler, ABC):
    """
    Create basic task handler
    """

    def post(self):
        """
        Handle task create process
        :return: task object
        """
        execution_type = self.get_argument("type", default="queue")
        try:
            body = json_decode(self.request.body)
            task = TaskItem()
            TaskCrud.create_task(task=task)
            if execution_type == "process":
                TaskSpawner.run_task(task, body)
            elif execution_type == "queue":
                publisher.publish_task(task=task, cases=body)
            else:
                return self.error(
                    msg=f"Unsupported value for parameter 'type': {execution_type}. "
                        f"Supported values: 'queue', 'process'"
                )
            response = json_encode(task.as_json())
        except Exception as create_task_err:
            return self.error(
                msg=f"Unexpected error at task creating: {str(create_task_err)}"
            )
        self.write(response)


class CreateTaskQueueHandler(BaseHandler, ABC):
    """
    Create basic task handler
    """

    def post(self):
        """
        Handle task create process
        :return: task object
        """
        try:
            body = json_decode(self.request.body)
            task = TaskItem()
            TaskCrud.create_task(task=task)
            publisher.publish_task(task=task, cases=body)
            response = json_encode(task.as_json())
        except Exception as create_task_err:
            return self.error(
                msg=f"Unexpected error at task creating: {str(create_task_err)}"
            )
        self.write(response)


class ListTaskHandler(BaseHandler, ABC):
    """
    Return tasks
    """

    def get(self) -> None:
        """
        Return tasks data
        :return: None
        """
        task_id = self.get_argument("task_id", default=None)
        limit = self.get_argument("limit", default=None)
        try:
            tasks = dumps(
                TaskCrud.get_task(task_id)
                if task_id
                else TaskCrud.get_tasks(int(limit) if limit else None),
                default=str,
            )
        except Exception as list_task_err:
            return self.error(
                msg=f"Unexpected error at tasks listing: {str(list_task_err)}"
            )
        self.write(tasks)


class ResultsHandler(BaseHandler, ABC):
    """
    Return results
    """

    def get(self) -> None:
        """
        Return results data
        :return: None
        """
        try:
            task_id = self.get_argument("task_id", default=None)
            redis_cache = redis.get(task_id)
            # If cache is available - write cache as response
            if redis_cache:
                logger.info(msg=f"Redis cache is available, task '{task_id}'")
                return self.write(redis_cache)
            # If cache is not available - get results from the database
            db_results = TaskCrud.get_results(task_id)
            json_results = dumps(db_results, default=str)
            # If status is 'pending' (in progress), skip cache saving, write database results
            if db_results.get("task", {}).get("status", "") == TaskStatus.PENDING:
                logger.info(msg=f"Status of the task '{task_id}' is '{TaskStatus.PENDING}', skip Redis cache saving")
                return self.write(json_results)
            # If status is 'error' or 'success' (finished in any way), save the cache and write database results
            redis.set(key=task_id, value=json_results)
            logger.info(msg=f"Save results to Redis cache, task '{task_id}'")
            self.write(json_results)
        except Exception as get_results_error:
            return self.error(
                msg=f"Unexpected error at getting results: {str(get_results_error)}"
            )


class HealthCheckHandler(BaseHandler, ABC):
    """
    Implement docker health check
    """

    def get(self) -> None:
        """
        Return service status
        :return: None
        """
        self.write({"status": "up"})


def make_app() -> Application:
    """
    Create application
    :return: Application
    """
    return Application(
        handlers=[
            (r"/api/tasks/create", CreateTaskHandler),
            (r"/api/tasks/list", ListTaskHandler),
            (r"/api/results", ResultsHandler),
            (r"/api/health", HealthCheckHandler),
        ]
    )


if __name__ == "__main__":
    # Enable logging
    parse_command_line()

    # Prepare database
    Base.metadata.create_all(Engine)

    # Create application
    app = make_app()
    app.listen(port=8888)

    # Init rabbitmq queue polling
    polling = tornado.ioloop.PeriodicCallback(
        lambda: publisher.process_data_events(), callback_time=1.000
    )
    polling.start()

    # Here we go!
    logger.info(msg="Server successfully started. Wait for incoming connections.")
    IOLoop.current().start()
