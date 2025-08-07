from __future__ import annotations
import weakref

import asyncio
import html
import csv
import json
import logging
import logging.handlers
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from hashlib import md5
from typing import Any, Protocol, Callable, TYPE_CHECKING, List
import numpy as np
from dotenv import load_dotenv
from lightrag.constants import (
    DEFAULT_LOG_MAX_BYTES,
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_FILENAME,
    GRAPH_FIELD_SEP,
    DEFAULT_MAX_TOTAL_TOKENS,
    DEFAULT_MAX_FILE_PATH_LENGTH,
)


def get_env_value(
    env_key: str, default: any, value_type: type = str, special_none: bool = False
) -> any:
    """
    Get value from environment variable with type conversion

    Args:
        env_key (str): Environment variable key
        default (any): Default value if env variable is not set
        value_type (type): Type to convert the value to
        special_none (bool): If True, return None when value is "None"

    Returns:
        any: Converted value from environment or default
    """
    value = os.getenv(env_key)
    if value is None:
        return default

    # Handle special case for "None" string
    if special_none and value == "None":
        return None

    if value_type is bool:
        return value.lower() in ("true", "1", "yes", "t", "on")
    try:
        return value_type(value)
    except (ValueError, TypeError):
        return default


# Use TYPE_CHECKING to avoid circular imports
if TYPE_CHECKING:
    from lightrag.base import BaseKVStorage, QueryParam

# use the .env that is inside the current folder
# allows to use different .env file for each lightrag instance
# the OS environment variables take precedence over the .env file
load_dotenv(dotenv_path=".env", override=False)

VERBOSE_DEBUG = os.getenv("VERBOSE", "false").lower() == "true"


def verbose_debug(msg: str, *args, **kwargs):
    """Function for outputting detailed debug information.
    When VERBOSE_DEBUG=True, outputs the complete message.
    When VERBOSE_DEBUG=False, outputs only the first 50 characters.

    Args:
        msg: The message format string
        *args: Arguments to be formatted into the message
        **kwargs: Keyword arguments passed to logger.debug()
    """
    if VERBOSE_DEBUG:
        logger.debug(msg, *args, **kwargs)
    else:
        # Format the message with args first
        if args:
            formatted_msg = msg % args
        else:
            formatted_msg = msg
        # Then truncate the formatted message
        truncated_msg = (
            formatted_msg[:150] + "..." if len(formatted_msg) > 150 else formatted_msg
        )
        # Remove consecutive newlines
        truncated_msg = re.sub(r"\n+", "\n", truncated_msg)
        logger.debug(truncated_msg, **kwargs)


def set_verbose_debug(enabled: bool):
    """Enable or disable verbose debug output"""
    global VERBOSE_DEBUG
    VERBOSE_DEBUG = enabled


statistic_data = {"llm_call": 0, "llm_cache": 0, "embed_call": 0}

# Initialize logger
logger = logging.getLogger("lightrag")
logger.propagate = False  # prevent log message send to root loggger
# Let the main application configure the handlers
logger.setLevel(logging.INFO)

# Set httpx logging level to WARNING
logging.getLogger("httpx").setLevel(logging.WARNING)


class LightragPathFilter(logging.Filter):
    """Filter for lightrag logger to filter out frequent path access logs"""

    def __init__(self):
        super().__init__()
        # Define paths to be filtered
        self.filtered_paths = [
            "/documents",
            "/documents/paginated",
            "/health",
            "/webui/",
            "/documents/pipeline_status",
        ]
        # self.filtered_paths = ["/health", "/webui/"]

    def filter(self, record):
        try:
            # Check if record has the required attributes for an access log
            if not hasattr(record, "args") or not isinstance(record.args, tuple):
                return True
            if len(record.args) < 5:
                return True

            # Extract method, path and status from the record args
            method = record.args[1]
            path = record.args[2]
            status = record.args[4]

            # Filter out successful GET requests to filtered paths
            if (
                method == "GET"
                or method == "POST"
                and (status == 200 or status == 304)
                and path in self.filtered_paths
            ):
                return False

            return True
        except Exception:
            # In case of any error, let the message through
            return True


def setup_logger(
    logger_name: str,
    level: str = "INFO",
    add_filter: bool = False,
    log_file_path: str | None = None,
    enable_file_logging: bool = True,
):
    """Set up a logger with console and optionally file handlers

    Args:
        logger_name: Name of the logger to set up
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        add_filter: Whether to add LightragPathFilter to the logger
        log_file_path: Path to the log file. If None and file logging is enabled, defaults to lightrag.log in LOG_DIR or cwd
        enable_file_logging: Whether to enable logging to a file (defaults to True)
    """
    # Configure formatters
    detailed_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    simple_formatter = logging.Formatter("%(levelname)s: %(message)s")

    logger_instance = logging.getLogger(logger_name)
    logger_instance.setLevel(level)
    logger_instance.handlers = []  # Clear existing handlers
    logger_instance.propagate = False

    # Add console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(simple_formatter)
    console_handler.setLevel(level)
    logger_instance.addHandler(console_handler)

    # Add file handler by default unless explicitly disabled
    if enable_file_logging:
        # Get log file path
        if log_file_path is None:
            log_dir = os.getenv("LOG_DIR", os.getcwd())
            log_file_path = os.path.abspath(os.path.join(log_dir, DEFAULT_LOG_FILENAME))

        # Ensure log directory exists
        os.makedirs(os.path.dirname(log_file_path), exist_ok=True)

        # Get log file max size and backup count from environment variables
        log_max_bytes = get_env_value("LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES, int)
        log_backup_count = get_env_value(
            "LOG_BACKUP_COUNT", DEFAULT_LOG_BACKUP_COUNT, int
        )

        try:
            # Add file handler
            file_handler = logging.handlers.RotatingFileHandler(
                filename=log_file_path,
                maxBytes=log_max_bytes,
                backupCount=log_backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(detailed_formatter)
            file_handler.setLevel(level)
            logger_instance.addHandler(file_handler)
        except PermissionError as e:
            logger.warning(f"Could not create log file at {log_file_path}: {str(e)}")
            logger.warning("Continuing with console logging only")

    # Add path filter if requested
    if add_filter:
        path_filter = LightragPathFilter()
        logger_instance.addFilter(path_filter)


class UnlimitedSemaphore:
    """A context manager that allows unlimited access."""

    async def __aenter__(self):
        pass

    async def __aexit__(self, exc_type, exc, tb):
        pass


@dataclass
class EmbeddingFunc:
    embedding_dim: int
    func: callable
    max_token_size: int | None = None  # deprecated keep it for compatible only

    async def __call__(self, *args, **kwargs) -> np.ndarray:
        return await self.func(*args, **kwargs)


def compute_args_hash(*args: Any) -> str:
    """Compute a hash for the given arguments.
    Args:
        *args: Arguments to hash
    Returns:
        str: Hash string
    """
    import hashlib

    # Convert all arguments to strings and join them
    args_str = "".join([str(arg) for arg in args])

    # Compute MD5 hash
    return hashlib.md5(args_str.encode()).hexdigest()


def generate_cache_key(mode: str, cache_type: str, hash_value: str) -> str:
    """Generate a flattened cache key in the format {mode}:{cache_type}:{hash}

    Args:
        mode: Cache mode (e.g., 'default', 'local', 'global')
        cache_type: Type of cache (e.g., 'extract', 'query', 'keywords')
        hash_value: Hash value from compute_args_hash

    Returns:
        str: Flattened cache key
    """
    return f"{mode}:{cache_type}:{hash_value}"


def parse_cache_key(cache_key: str) -> tuple[str, str, str] | None:
    """Parse a flattened cache key back into its components

    Args:
        cache_key: Flattened cache key in format {mode}:{cache_type}:{hash}

    Returns:
        tuple[str, str, str] | None: (mode, cache_type, hash) or None if invalid format
    """
    parts = cache_key.split(":", 2)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return None


def compute_mdhash_id(content: str, prefix: str = "") -> str:
    """
    Compute a unique ID for a given content string.

    The ID is a combination of the given prefix and the MD5 hash of the content string.
    """
    return prefix + md5(content.encode()).hexdigest()


# Custom exception class
class QueueFullError(Exception):
    """Raised when the queue is full and the wait times out"""

    pass


def priority_limit_async_func_call(max_size: int, max_queue_size: int = 1000):
    """
    Enhanced priority-limited asynchronous function call decorator

    Args:
        max_size: Maximum number of concurrent calls
        max_queue_size: Maximum queue capacity to prevent memory overflow
    Returns:
        Decorator function
    """

    def final_decro(func):
        # Ensure func is callable
        if not callable(func):
            raise TypeError(f"Expected a callable object, got {type(func)}")
        queue = asyncio.PriorityQueue(maxsize=max_queue_size)
        tasks = set()
        initialization_lock = asyncio.Lock()
        counter = 0
        shutdown_event = asyncio.Event()
        initialized = False  # Global initialization flag
        worker_health_check_task = None

        # Track active future objects for cleanup
        active_futures = weakref.WeakSet()
        reinit_count = 0  # Reinitialization counter to track system health

        # Worker function to process tasks in the queue
        async def worker():
            """Worker that processes tasks in the priority queue"""
            try:
                while not shutdown_event.is_set():
                    try:
                        # Use timeout to get tasks, allowing periodic checking of shutdown signal
                        try:
                            (
                                priority,
                                count,
                                future,
                                args,
                                kwargs,
                            ) = await asyncio.wait_for(queue.get(), timeout=1.0)
                        except asyncio.TimeoutError:
                            # Timeout is just to check shutdown signal, continue to next iteration
                            continue

                        # If future is cancelled, skip execution
                        if future.cancelled():
                            queue.task_done()
                            continue

                        try:
                            # Execute function
                            result = await func(*args, **kwargs)
                            # If future is not done, set the result
                            if not future.done():
                                future.set_result(result)
                        except asyncio.CancelledError:
                            if not future.done():
                                future.cancel()
                            logger.debug("limit_async: Task cancelled during execution")
                        except Exception as e:
                            logger.error(
                                f"limit_async: Error in decorated function: {str(e)}"
                            )
                            if not future.done():
                                future.set_exception(e)
                        finally:
                            queue.task_done()
                    except Exception as e:
                        # Catch all exceptions in worker loop to prevent worker termination
                        logger.error(f"limit_async: Critical error in worker: {str(e)}")
                        await asyncio.sleep(0.1)  # Prevent high CPU usage
            finally:
                logger.debug("limit_async: Worker exiting")

        async def health_check():
            """Periodically check worker health status and recover"""
            nonlocal initialized
            try:
                while not shutdown_event.is_set():
                    await asyncio.sleep(5)  # Check every 5 seconds

                    # No longer acquire lock, directly operate on task set
                    # Use a copy of the task set to avoid concurrent modification
                    current_tasks = set(tasks)
                    done_tasks = {t for t in current_tasks if t.done()}
                    tasks.difference_update(done_tasks)

                    # Calculate active tasks count
                    active_tasks_count = len(tasks)
                    workers_needed = max_size - active_tasks_count

                    if workers_needed > 0:
                        logger.info(
                            f"limit_async: Creating {workers_needed} new workers"
                        )
                        new_tasks = set()
                        for _ in range(workers_needed):
                            task = asyncio.create_task(worker())
                            new_tasks.add(task)
                            task.add_done_callback(tasks.discard)
                        # Update task set in one operation
                        tasks.update(new_tasks)
            except Exception as e:
                logger.error(f"limit_async: Error in health check: {str(e)}")
            finally:
                logger.debug("limit_async: Health check task exiting")
                initialized = False

        async def ensure_workers():
            """Ensure worker threads and health check system are available

            This function checks if the worker system is already initialized.
            If not, it performs a one-time initialization of all worker threads
            and starts the health check system.
            """
            nonlocal initialized, worker_health_check_task, tasks, reinit_count

            if initialized:
                return

            async with initialization_lock:
                if initialized:
                    return

                # Increment reinitialization counter if this is not the first initialization
                if reinit_count > 0:
                    reinit_count += 1
                    logger.warning(
                        f"limit_async: Reinitializing needed (count: {reinit_count})"
                    )
                else:
                    reinit_count = 1  # First initialization

                # Check for completed tasks and remove them from the task set
                current_tasks = set(tasks)
                done_tasks = {t for t in current_tasks if t.done()}
                tasks.difference_update(done_tasks)

                # Log active tasks count during reinitialization
                active_tasks_count = len(tasks)
                if active_tasks_count > 0 and reinit_count > 1:
                    logger.warning(
                        f"limit_async: {active_tasks_count} tasks still running during reinitialization"
                    )

                # Create initial worker tasks, only adding the number needed
                workers_needed = max_size - active_tasks_count
                for _ in range(workers_needed):
                    task = asyncio.create_task(worker())
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)

                # Start health check
                worker_health_check_task = asyncio.create_task(health_check())

                initialized = True
                logger.info(f"limit_async: {workers_needed} new workers initialized")

        async def shutdown():
            """Gracefully shut down all workers and the queue"""
            logger.info("limit_async: Shutting down priority queue workers")

            # Set the shutdown event
            shutdown_event.set()

            # Cancel all active futures
            for future in list(active_futures):
                if not future.done():
                    future.cancel()

            # Wait for the queue to empty
            try:
                await asyncio.wait_for(queue.join(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "limit_async: Timeout waiting for queue to empty during shutdown"
                )

            # Cancel all worker tasks
            for task in list(tasks):
                if not task.done():
                    task.cancel()

            # Wait for all tasks to complete
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            # Cancel the health check task
            if worker_health_check_task and not worker_health_check_task.done():
                worker_health_check_task.cancel()
                try:
                    await worker_health_check_task
                except asyncio.CancelledError:
                    pass

            logger.info("limit_async: Priority queue workers shutdown complete")

        @wraps(func)
        async def wait_func(
            *args, _priority=10, _timeout=None, _queue_timeout=None, **kwargs
        ):
            """
            Execute the function with priority-based concurrency control
            Args:
                *args: Positional arguments passed to the function
                _priority: Call priority (lower values have higher priority)
                _timeout: Maximum time to wait for function completion (in seconds)
                _queue_timeout: Maximum time to wait for entering the queue (in seconds)
                **kwargs: Keyword arguments passed to the function
            Returns:
                The result of the function call
            Raises:
                TimeoutError: If the function call times out
                QueueFullError: If the queue is full and waiting times out
                Any exception raised by the decorated function
            """
            # Ensure worker system is initialized
            await ensure_workers()

            # Create a future for the result
            future = asyncio.Future()
            active_futures.add(future)

            nonlocal counter
            async with initialization_lock:
                current_count = counter  # Use local variable to avoid race conditions
                counter += 1

            # Try to put the task into the queue, supporting timeout
            try:
                if _queue_timeout is not None:
                    # Use timeout to wait for queue space
                    try:
                        await asyncio.wait_for(
                            # current_count is used to ensure FIFO order
                            queue.put((_priority, current_count, future, args, kwargs)),
                            timeout=_queue_timeout,
                        )
                    except asyncio.TimeoutError:
                        raise QueueFullError(
                            f"Queue full, timeout after {_queue_timeout} seconds"
                        )
                else:
                    # No timeout, may wait indefinitely
                    # current_count is used to ensure FIFO order
                    await queue.put((_priority, current_count, future, args, kwargs))
            except Exception as e:
                # Clean up the future
                if not future.done():
                    future.set_exception(e)
                active_futures.discard(future)
                raise

            try:
                # Wait for the result, optional timeout
                if _timeout is not None:
                    try:
                        return await asyncio.wait_for(future, _timeout)
                    except asyncio.TimeoutError:
                        # Cancel the future
                        if not future.done():
                            future.cancel()
                        raise TimeoutError(
                            f"limit_async: Task timed out after {_timeout} seconds"
                        )
                else:
                    # Wait for the result without timeout
                    return await future
            finally:
                # Clean up the future reference
                active_futures.discard(future)

        # Add the shutdown method to the decorated function
        wait_func.shutdown = shutdown

        return wait_func

    return final_decro


def wrap_embedding_func_with_attrs(**kwargs):
    """Wrap a function with attributes"""

    def final_decro(func) -> EmbeddingFunc:
        new_func = EmbeddingFunc(**kwargs, func=func)
        return new_func

    return final_decro


def load_json(file_name):
    if not os.path.exists(file_name):
        return None
    with open(file_name, encoding="utf-8") as f:
        return json.load(f)


def write_json(json_obj, file_name):
    with open(file_name, "w", encoding="utf-8") as f:
        json.dump(json_obj, f, indent=2, ensure_ascii=False)


class TokenizerInterface(Protocol):
    """
    Defines the interface for a tokenizer, requiring encode and decode methods.
    """

    def encode(self, content: str) -> List[int]:
        """Encodes a string into a list of tokens."""
        ...

    def decode(self, tokens: List[int]) -> str:
        """Decodes a list of tokens into a string."""
        ...


class Tokenizer:
    """
    A wrapper around a tokenizer to provide a consistent interface for encoding and decoding.
    """

    def __init__(self, model_name: str, tokenizer: TokenizerInterface):
        """
        Initializes the Tokenizer with a tokenizer model name and a tokenizer instance.

        Args:
            model_name: The associated model name for the tokenizer.
            tokenizer: An instance of a class implementing the TokenizerInterface.
        """
        self.model_name: str = model_name
        self.tokenizer: TokenizerInterface = tokenizer

    def encode(self, content: str) -> List[int]:
        """
        Encodes a string into a list of tokens using the underlying tokenizer.

        Args:
            content: The string to encode.

        Returns:
            A list of integer tokens.
        """
        return self.tokenizer.encode(content)

    def decode(self, tokens: List[int]) -> str:
        """
        Decodes a list of tokens into a string using the underlying tokenizer.

        Args:
            tokens: A list of integer tokens to decode.

        Returns:
            The decoded string.
        """
        return self.tokenizer.decode(tokens)


class TiktokenTokenizer(Tokenizer):
    """
    A Tokenizer implementation using the tiktoken library.
    """

    def __init__(self, model_name: str = "gpt-4o-mini"):
        """
        Initializes the TiktokenTokenizer with a specified model name.

        Args:
            model_name: The model name for the tiktoken tokenizer to use.  Defaults to "gpt-4o-mini".

        Raises:
            ImportError: If tiktoken is not installed.
            ValueError: If the model_name is invalid.
        """
        try:
            import tiktoken # 使用OpenAI的tiktoken库
        except ImportError:
            raise ImportError(
                "tiktoken is not installed. Please install it with `pip install tiktoken` or define custom `tokenizer_func`."
            )

        try:
            tokenizer = tiktoken.encoding_for_model(model_name)
            super().__init__(model_name=model_name, tokenizer=tokenizer)
        except KeyError:
            raise ValueError(f"Invalid model_name: {model_name}.")


def pack_user_ass_to_openai_messages(*args: str):
    roles = ["user", "assistant"]
    return [
        {"role": roles[i % 2], "content": content} for i, content in enumerate(args)
    ]


def split_string_by_multi_markers(content: str, markers: list[str]) -> list[str]:
    """Split a string by multiple markers"""
    if not markers:
        return [content]
    content = content if content is not None else ""
    results = re.split("|".join(re.escape(marker) for marker in markers), content)
    return [r.strip() for r in results if r.strip()]


# Refer the utils functions of the official GraphRAG implementation:
# https://github.com/microsoft/graphrag
def clean_str(input: Any) -> str:
    """Clean an input string by removing HTML escapes, control characters, and other unwanted characters."""
    # If we get non-string input, just give it back
    if not isinstance(input, str):
        return input

    result = html.unescape(input.strip())
    # https://stackoverflow.com/questions/4324790/removing-control-characters-from-a-string-in-python
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "", result)


def is_float_regex(value: str) -> bool:
    return bool(re.match(r"^[-+]?[0-9]*\.?[0-9]+$", value))


def truncate_list_by_token_size(
    list_data: list[Any],
    key: Callable[[Any], str],
    max_token_size: int,
    tokenizer: Tokenizer,
) -> list[int]:
    """Truncate a list of data by token size"""
    if max_token_size <= 0:
        return []
    tokens = 0
    for i, data in enumerate(list_data):
        tokens += len(tokenizer.encode(key(data)))
        if tokens > max_token_size:
            return list_data[:i]
    return list_data


def cosine_similarity(v1, v2):
    """Calculate cosine similarity between two vectors"""
    dot_product = np.dot(v1, v2)
    norm1 = np.linalg.norm(v1)
    norm2 = np.linalg.norm(v2)
    return dot_product / (norm1 * norm2)


async def handle_cache(
    hashing_kv,
    args_hash,
    prompt,
    mode="default",
    cache_type=None,
):
    """Generic cache handling function with flattened cache keys"""
    if hashing_kv is None:
        return None, None, None, None

    if mode != "default":  # handle cache for all type of query
        if not hashing_kv.global_config.get("enable_llm_cache"):
            return None, None, None, None
    else:  # handle cache for entity extraction
        if not hashing_kv.global_config.get("enable_llm_cache_for_entity_extract"):
            return None, None, None, None

    # Use flattened cache key format: {mode}:{cache_type}:{hash}
    flattened_key = generate_cache_key(mode, cache_type, args_hash)
    cache_entry = await hashing_kv.get_by_id(flattened_key)
    if cache_entry:
        logger.debug(f"Flattened cache hit(key:{flattened_key})")
        return cache_entry["return"], None, None, None

    logger.debug(f"Cache missed(mode:{mode} type:{cache_type})")
    return None, None, None, None


@dataclass
class CacheData:
    args_hash: str
    content: str
    prompt: str
    mode: str = "default"
    cache_type: str = "query"
    chunk_id: str | None = None
    queryparam: dict | None = None


async def save_to_cache(hashing_kv, cache_data: CacheData):
    """Save data to cache using flattened key structure.

    Args:
        hashing_kv: The key-value storage for caching
        cache_data: The cache data to save
    """
    # Skip if storage is None or content is a streaming response
    if hashing_kv is None or not cache_data.content:
        return

    # If content is a streaming response, don't cache it
    if hasattr(cache_data.content, "__aiter__"):
        logger.debug("Streaming response detected, skipping cache")
        return

    # Use flattened cache key format: {mode}:{cache_type}:{hash}
    flattened_key = generate_cache_key(
        cache_data.mode, cache_data.cache_type, cache_data.args_hash
    )

    # Check if we already have identical content cached
    existing_cache = await hashing_kv.get_by_id(flattened_key)
    if existing_cache:
        existing_content = existing_cache.get("return")
        if existing_content == cache_data.content:
            logger.info(f"Cache content unchanged for {flattened_key}, skipping update")
            return

    # Create cache entry with flattened structure
    cache_entry = {
        "return": cache_data.content,
        "cache_type": cache_data.cache_type,
        "chunk_id": cache_data.chunk_id if cache_data.chunk_id is not None else None,
        "original_prompt": cache_data.prompt,
        "queryparam": cache_data.queryparam
        if cache_data.queryparam is not None
        else None,
    }

    logger.info(f" == LLM cache == saving: {flattened_key}")

    # Save using flattened key
    await hashing_kv.upsert({flattened_key: cache_entry})


def safe_unicode_decode(content):
    # Regular expression to find all Unicode escape sequences of the form \uXXXX
    unicode_escape_pattern = re.compile(r"\\u([0-9a-fA-F]{4})")

    # Function to replace the Unicode escape with the actual character
    def replace_unicode_escape(match):
        # Convert the matched hexadecimal value into the actual Unicode character
        return chr(int(match.group(1), 16))

    # Perform the substitution
    decoded_content = unicode_escape_pattern.sub(
        replace_unicode_escape, content.decode("utf-8")
    )

    return decoded_content


def exists_func(obj, func_name: str) -> bool:
    """Check if a function exists in an object or not.
    :param obj:
    :param func_name:
    :return: True / False
    """
    if callable(getattr(obj, func_name, None)):
        return True
    else:
        return False


def get_conversation_turns(
    conversation_history: list[dict[str, Any]], num_turns: int
) -> str:
    """
    Process conversation history to get the specified number of complete turns.

    Args:
        conversation_history: List of conversation messages in chronological order
        num_turns: Number of complete turns to include

    Returns:
        Formatted string of the conversation history
    """
    # Check if num_turns is valid
    if num_turns <= 0:
        return ""

    # Group messages into turns
    turns: list[list[dict[str, Any]]] = []
    messages: list[dict[str, Any]] = []

    # First, filter out keyword extraction messages
    for msg in conversation_history:
        if msg["role"] == "assistant" and (
            msg["content"].startswith('{ "high_level_keywords"')
            or msg["content"].startswith("{'high_level_keywords'")
        ):
            continue
        messages.append(msg)

    # Then process messages in chronological order
    i = 0
    while i < len(messages) - 1:
        msg1 = messages[i]
        msg2 = messages[i + 1]

        # Check if we have a user-assistant or assistant-user pair
        if (msg1["role"] == "user" and msg2["role"] == "assistant") or (
            msg1["role"] == "assistant" and msg2["role"] == "user"
        ):
            # Always put user message first in the turn
            if msg1["role"] == "assistant":
                turn = [msg2, msg1]  # user, assistant
            else:
                turn = [msg1, msg2]  # user, assistant
            turns.append(turn)
        i += 2

    # Keep only the most recent num_turns
    if len(turns) > num_turns:
        turns = turns[-num_turns:]

    # Format the turns into a string
    formatted_turns: list[str] = []
    for turn in turns:
        formatted_turns.extend(
            [f"user: {turn[0]['content']}", f"assistant: {turn[1]['content']}"]
        )

    return "\n".join(formatted_turns)


def always_get_an_event_loop() -> asyncio.AbstractEventLoop:
    """
    Ensure that there is always an event loop available.

    This function tries to get the current event loop. If the current event loop is closed or does not exist,
    it creates a new event loop and sets it as the current event loop.

    Returns:
        asyncio.AbstractEventLoop: The current or newly created event loop.
    """
    try:
        # Try to get the current event loop
        current_loop = asyncio.get_event_loop()
        if current_loop.is_closed():
            raise RuntimeError("Event loop is closed.")
        return current_loop

    except RuntimeError:
        # If no event loop exists or it is closed, create a new one
        logger.info("Creating a new event loop in main thread.")
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        return new_loop


async def aexport_data(
    chunk_entity_relation_graph,
    entities_vdb,
    relationships_vdb,
    output_path: str,
    file_format: str = "csv",
    include_vector_data: bool = False,
) -> None:
    """
    Asynchronously exports all entities, relations, and relationships to various formats.

    Args:
        chunk_entity_relation_graph: Graph storage instance for entities and relations
        entities_vdb: Vector database storage for entities
        relationships_vdb: Vector database storage for relationships
        output_path: The path to the output file (including extension).
        file_format: Output format - "csv", "excel", "md", "txt".
            - csv: Comma-separated values file
            - excel: Microsoft Excel file with multiple sheets
            - md: Markdown tables
            - txt: Plain text formatted output
        include_vector_data: Whether to include data from the vector database.
    """
    # Collect data
    entities_data = []
    relations_data = []
    relationships_data = []

    # --- Entities ---
    all_entities = await chunk_entity_relation_graph.get_all_labels()
    for entity_name in all_entities:
        # Get entity information from graph
        node_data = await chunk_entity_relation_graph.get_node(entity_name)
        source_id = node_data.get("source_id") if node_data else None

        entity_info = {
            "graph_data": node_data,
            "source_id": source_id,
        }

        # Optional: Get vector database information
        if include_vector_data:
            entity_id = compute_mdhash_id(entity_name, prefix="ent-")
            vector_data = await entities_vdb.get_by_id(entity_id)
            entity_info["vector_data"] = vector_data

        entity_row = {
            "entity_name": entity_name,
            "source_id": source_id,
            "graph_data": str(
                entity_info["graph_data"]
            ),  # Convert to string to ensure compatibility
        }
        if include_vector_data and "vector_data" in entity_info:
            entity_row["vector_data"] = str(entity_info["vector_data"])
        entities_data.append(entity_row)

    # --- Relations ---
    for src_entity in all_entities:
        for tgt_entity in all_entities:
            if src_entity == tgt_entity:
                continue

            edge_exists = await chunk_entity_relation_graph.has_edge(
                src_entity, tgt_entity
            )
            if edge_exists:
                # Get edge information from graph
                edge_data = await chunk_entity_relation_graph.get_edge(
                    src_entity, tgt_entity
                )
                source_id = edge_data.get("source_id") if edge_data else None

                relation_info = {
                    "graph_data": edge_data,
                    "source_id": source_id,
                }

                # Optional: Get vector database information
                if include_vector_data:
                    rel_id = compute_mdhash_id(src_entity + tgt_entity, prefix="rel-")
                    vector_data = await relationships_vdb.get_by_id(rel_id)
                    relation_info["vector_data"] = vector_data

                relation_row = {
                    "src_entity": src_entity,
                    "tgt_entity": tgt_entity,
                    "source_id": relation_info["source_id"],
                    "graph_data": str(relation_info["graph_data"]),  # Convert to string
                }
                if include_vector_data and "vector_data" in relation_info:
                    relation_row["vector_data"] = str(relation_info["vector_data"])
                relations_data.append(relation_row)

    # --- Relationships (from VectorDB) ---
    all_relationships = await relationships_vdb.client_storage
    for rel in all_relationships["data"]:
        relationships_data.append(
            {
                "relationship_id": rel["__id__"],
                "data": str(rel),  # Convert to string for compatibility
            }
        )

    # Export based on format
    if file_format == "csv":
        # CSV export
        with open(output_path, "w", newline="", encoding="utf-8") as csvfile:
            # Entities
            if entities_data:
                csvfile.write("# ENTITIES\n")
                writer = csv.DictWriter(csvfile, fieldnames=entities_data[0].keys())
                writer.writeheader()
                writer.writerows(entities_data)
                csvfile.write("\n\n")

            # Relations
            if relations_data:
                csvfile.write("# RELATIONS\n")
                writer = csv.DictWriter(csvfile, fieldnames=relations_data[0].keys())
                writer.writeheader()
                writer.writerows(relations_data)
                csvfile.write("\n\n")

            # Relationships
            if relationships_data:
                csvfile.write("# RELATIONSHIPS\n")
                writer = csv.DictWriter(
                    csvfile, fieldnames=relationships_data[0].keys()
                )
                writer.writeheader()
                writer.writerows(relationships_data)

    elif file_format == "excel":
        # Excel export
        import pandas as pd

        entities_df = pd.DataFrame(entities_data) if entities_data else pd.DataFrame()
        relations_df = (
            pd.DataFrame(relations_data) if relations_data else pd.DataFrame()
        )
        relationships_df = (
            pd.DataFrame(relationships_data) if relationships_data else pd.DataFrame()
        )

        with pd.ExcelWriter(output_path, engine="xlsxwriter") as writer:
            if not entities_df.empty:
                entities_df.to_excel(writer, sheet_name="Entities", index=False)
            if not relations_df.empty:
                relations_df.to_excel(writer, sheet_name="Relations", index=False)
            if not relationships_df.empty:
                relationships_df.to_excel(
                    writer, sheet_name="Relationships", index=False
                )

    elif file_format == "md":
        # Markdown export
        with open(output_path, "w", encoding="utf-8") as mdfile:
            mdfile.write("# LightRAG Data Export\n\n")

            # Entities
            mdfile.write("## Entities\n\n")
            if entities_data:
                # Write header
                mdfile.write("| " + " | ".join(entities_data[0].keys()) + " |\n")
                mdfile.write(
                    "| " + " | ".join(["---"] * len(entities_data[0].keys())) + " |\n"
                )

                # Write rows
                for entity in entities_data:
                    mdfile.write(
                        "| " + " | ".join(str(v) for v in entity.values()) + " |\n"
                    )
                mdfile.write("\n\n")
            else:
                mdfile.write("*No entity data available*\n\n")

            # Relations
            mdfile.write("## Relations\n\n")
            if relations_data:
                # Write header
                mdfile.write("| " + " | ".join(relations_data[0].keys()) + " |\n")
                mdfile.write(
                    "| " + " | ".join(["---"] * len(relations_data[0].keys())) + " |\n"
                )

                # Write rows
                for relation in relations_data:
                    mdfile.write(
                        "| " + " | ".join(str(v) for v in relation.values()) + " |\n"
                    )
                mdfile.write("\n\n")
            else:
                mdfile.write("*No relation data available*\n\n")

            # Relationships
            mdfile.write("## Relationships\n\n")
            if relationships_data:
                # Write header
                mdfile.write("| " + " | ".join(relationships_data[0].keys()) + " |\n")
                mdfile.write(
                    "| "
                    + " | ".join(["---"] * len(relationships_data[0].keys()))
                    + " |\n"
                )

                # Write rows
                for relationship in relationships_data:
                    mdfile.write(
                        "| "
                        + " | ".join(str(v) for v in relationship.values())
                        + " |\n"
                    )
            else:
                mdfile.write("*No relationship data available*\n\n")

    elif file_format == "txt":
        # Plain text export
        with open(output_path, "w", encoding="utf-8") as txtfile:
            txtfile.write("LIGHTRAG DATA EXPORT\n")
            txtfile.write("=" * 80 + "\n\n")

            # Entities
            txtfile.write("ENTITIES\n")
            txtfile.write("-" * 80 + "\n")
            if entities_data:
                # Create fixed width columns
                col_widths = {
                    k: max(len(k), max(len(str(e[k])) for e in entities_data))
                    for k in entities_data[0]
                }
                header = "  ".join(k.ljust(col_widths[k]) for k in entities_data[0])
                txtfile.write(header + "\n")
                txtfile.write("-" * len(header) + "\n")

                # Write rows
                for entity in entities_data:
                    row = "  ".join(
                        str(v).ljust(col_widths[k]) for k, v in entity.items()
                    )
                    txtfile.write(row + "\n")
                txtfile.write("\n\n")
            else:
                txtfile.write("No entity data available\n\n")

            # Relations
            txtfile.write("RELATIONS\n")
            txtfile.write("-" * 80 + "\n")
            if relations_data:
                # Create fixed width columns
                col_widths = {
                    k: max(len(k), max(len(str(r[k])) for r in relations_data))
                    for k in relations_data[0]
                }
                header = "  ".join(k.ljust(col_widths[k]) for k in relations_data[0])
                txtfile.write(header + "\n")
                txtfile.write("-" * len(header) + "\n")

                # Write rows
                for relation in relations_data:
                    row = "  ".join(
                        str(v).ljust(col_widths[k]) for k, v in relation.items()
                    )
                    txtfile.write(row + "\n")
                txtfile.write("\n\n")
            else:
                txtfile.write("No relation data available\n\n")

            # Relationships
            txtfile.write("RELATIONSHIPS\n")
            txtfile.write("-" * 80 + "\n")
            if relationships_data:
                # Create fixed width columns
                col_widths = {
                    k: max(len(k), max(len(str(r[k])) for r in relationships_data))
                    for k in relationships_data[0]
                }
                header = "  ".join(
                    k.ljust(col_widths[k]) for k in relationships_data[0]
                )
                txtfile.write(header + "\n")
                txtfile.write("-" * len(header) + "\n")

                # Write rows
                for relationship in relationships_data:
                    row = "  ".join(
                        str(v).ljust(col_widths[k]) for k, v in relationship.items()
                    )
                    txtfile.write(row + "\n")
            else:
                txtfile.write("No relationship data available\n\n")

    else:
        raise ValueError(
            f"Unsupported file format: {file_format}. "
            f"Choose from: csv, excel, md, txt"
        )
    if file_format is not None:
        print(f"Data exported to: {output_path} with format: {file_format}")
    else:
        print("Data displayed as table format")


def export_data(
    chunk_entity_relation_graph,
    entities_vdb,
    relationships_vdb,
    output_path: str,
    file_format: str = "csv",
    include_vector_data: bool = False,
) -> None:
    """
    Synchronously exports all entities, relations, and relationships to various formats.

    Args:
        chunk_entity_relation_graph: Graph storage instance for entities and relations
        entities_vdb: Vector database storage for entities
        relationships_vdb: Vector database storage for relationships
        output_path: The path to the output file (including extension).
        file_format: Output format - "csv", "excel", "md", "txt".
            - csv: Comma-separated values file
            - excel: Microsoft Excel file with multiple sheets
            - md: Markdown tables
            - txt: Plain text formatted output
        include_vector_data: Whether to include data from the vector database.
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    loop.run_until_complete(
        aexport_data(
            chunk_entity_relation_graph,
            entities_vdb,
            relationships_vdb,
            output_path,
            file_format,
            include_vector_data,
        )
    )


def lazy_external_import(module_name: str, class_name: str) -> Callable[..., Any]:
    """Lazily import a class from an external module based on the package of the caller."""
    # Get the caller's module and package
    import inspect

    caller_frame = inspect.currentframe().f_back
    module = inspect.getmodule(caller_frame)
    package = module.__package__ if module else None

    def import_class(*args: Any, **kwargs: Any):
        import importlib

        module = importlib.import_module(module_name, package=package)
        cls = getattr(module, class_name)
        return cls(*args, **kwargs)

    return import_class


async def update_chunk_cache_list(
    chunk_id: str,
    text_chunks_storage: "BaseKVStorage",
    cache_keys: list[str],
    cache_scenario: str = "batch_update",
) -> None:
    """Update chunk's llm_cache_list with the given cache keys

    Args:
        chunk_id: Chunk identifier
        text_chunks_storage: Text chunks storage instance
        cache_keys: List of cache keys to add to the list
        cache_scenario: Description of the cache scenario for logging
    """
    if not cache_keys:
        return

    try:
        chunk_data = await text_chunks_storage.get_by_id(chunk_id)
        if chunk_data:
            # Ensure llm_cache_list exists
            if "llm_cache_list" not in chunk_data:
                chunk_data["llm_cache_list"] = []

            # Add cache keys to the list if not already present
            existing_keys = set(chunk_data["llm_cache_list"])
            new_keys = [key for key in cache_keys if key not in existing_keys]

            if new_keys:
                chunk_data["llm_cache_list"].extend(new_keys)

                # Update the chunk in storage
                await text_chunks_storage.upsert({chunk_id: chunk_data})
                logger.debug(
                    f"Updated chunk {chunk_id} with {len(new_keys)} cache keys ({cache_scenario})"
                )
    except Exception as e:
        logger.warning(
            f"Failed to update chunk {chunk_id} with cache references on {cache_scenario}: {e}"
        )


def remove_think_tags(text: str) -> str:
    """Remove <think> tags from the text"""
    return re.sub(r"^(<think>.*?</think>|<think>)", "", text, flags=re.DOTALL).strip()


async def use_llm_func_with_cache(
    input_text: str,
    use_llm_func: callable,
    llm_response_cache: "BaseKVStorage | None" = None,
    max_tokens: int = None,
    history_messages: list[dict[str, str]] = None,
    cache_type: str = "extract",
    chunk_id: str | None = None,
    cache_keys_collector: list = None,
) -> str:
    """Call LLM function with cache support

    If cache is available and enabled (determined by handle_cache based on mode),
    retrieve result from cache; otherwise call LLM function and save result to cache.

    Args:
        input_text: Input text to send to LLM
        use_llm_func: LLM function with higher priority
        llm_response_cache: Cache storage instance
        max_tokens: Maximum tokens for generation
        history_messages: History messages list
        cache_type: Type of cache
        chunk_id: Chunk identifier to store in cache
        text_chunks_storage: Text chunks storage to update llm_cache_list
        cache_keys_collector: Optional list to collect cache keys for batch processing

    Returns:
        LLM response text
    """
    if llm_response_cache:
        if history_messages:
            history = json.dumps(history_messages, ensure_ascii=False)
            _prompt = history + "\n" + input_text
        else:
            _prompt = input_text

        arg_hash = compute_args_hash(_prompt)
        # Generate cache key for this LLM call
        cache_key = generate_cache_key("default", cache_type, arg_hash)

        cached_return, _1, _2, _3 = await handle_cache(
            llm_response_cache,
            arg_hash,
            _prompt,
            "default",
            cache_type=cache_type,
        )
        if cached_return:
            logger.debug(f"Found cache for {arg_hash}")
            statistic_data["llm_cache"] += 1

            # Add cache key to collector if provided
            if cache_keys_collector is not None:
                cache_keys_collector.append(cache_key)

            return cached_return
        statistic_data["llm_call"] += 1

        # Call LLM
        kwargs = {}
        if history_messages:
            kwargs["history_messages"] = history_messages
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        res: str = await use_llm_func(input_text, **kwargs)
        res = remove_think_tags(res)

        if llm_response_cache.global_config.get("enable_llm_cache_for_entity_extract"):
            await save_to_cache(
                llm_response_cache,
                CacheData(
                    args_hash=arg_hash,
                    content=res,
                    prompt=_prompt,
                    cache_type=cache_type,
                    chunk_id=chunk_id,
                ),
            )

            # Add cache key to collector if provided
            if cache_keys_collector is not None:
                cache_keys_collector.append(cache_key)

        return res

    # When cache is disabled, directly call LLM
    kwargs = {}
    if history_messages:
        kwargs["history_messages"] = history_messages
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    logger.info(f"Call LLM function with query text length: {len(input_text)}")
    res = await use_llm_func(input_text, **kwargs)
    return remove_think_tags(res)


def get_content_summary(content: str, max_length: int = 250) -> str:
    """Get summary of document content

    Args:
        content: Original document content
        max_length: Maximum length of summary

    Returns:
        Truncated content with ellipsis if needed
    """
    content = content.strip()
    if len(content) <= max_length:
        return content
    return content[:max_length] + "..."


def normalize_extracted_info(name: str, is_entity=False) -> str:
    """Normalize entity/relation names and description with the following rules:
    1. Remove spaces between Chinese characters
    2. Remove spaces between Chinese characters and English letters/numbers
    3. Preserve spaces within English text and numbers
    4. Replace Chinese parentheses with English parentheses
    5. Replace Chinese dash with English dash
    6. Remove English quotation marks from the beginning and end of the text
    7. Remove English quotation marks in and around chinese
    8. Remove Chinese quotation marks

    Args:
        name: Entity name to normalize

    Returns:
        Normalized entity name
    """
    # Replace Chinese parentheses with English parentheses
    name = name.replace("（", "(").replace("）", ")")

    # Replace Chinese dash with English dash
    name = name.replace("—", "-").replace("－", "-")

    # Use regex to remove spaces between Chinese characters
    # Regex explanation:
    # (?<=[\u4e00-\u9fa5]): Positive lookbehind for Chinese character
    # \s+: One or more whitespace characters
    # (?=[\u4e00-\u9fa5]): Positive lookahead for Chinese character
    name = re.sub(r"(?<=[\u4e00-\u9fa5])\s+(?=[\u4e00-\u9fa5])", "", name)

    # Remove spaces between Chinese and English/numbers/symbols
    name = re.sub(
        r"(?<=[\u4e00-\u9fa5])\s+(?=[a-zA-Z0-9\(\)\[\]@#$%!&\*\-=+_])", "", name
    )
    name = re.sub(
        r"(?<=[a-zA-Z0-9\(\)\[\]@#$%!&\*\-=+_])\s+(?=[\u4e00-\u9fa5])", "", name
    )

    # Remove English quotation marks from the beginning and end
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    if len(name) >= 2 and name.startswith("'") and name.endswith("'"):
        name = name[1:-1]

    if is_entity:
        # remove Chinese quotes
        name = name.replace("“", "").replace("”", "").replace("‘", "").replace("’", "")
        # remove English queotes in and around chinese
        name = re.sub(r"['\"]+(?=[\u4e00-\u9fa5])", "", name)
        name = re.sub(r"(?<=[\u4e00-\u9fa5])['\"]+", "", name)

    return name


def clean_text(text: str) -> str:
    """Clean text by removing null bytes (0x00) and whitespace

    Args:
        text: Input text to clean

    Returns:
        Cleaned text
    """
    return text.strip().replace("\x00", "")


def check_storage_env_vars(storage_name: str) -> None:
    """Check if all required environment variables for storage implementation exist

    Args:
        storage_name: Storage implementation name

    Raises:
        ValueError: If required environment variables are missing
    """
    from lightrag.kg import STORAGE_ENV_REQUIREMENTS

    required_vars = STORAGE_ENV_REQUIREMENTS.get(storage_name, [])
    missing_vars = [var for var in required_vars if var not in os.environ]

    if missing_vars:
        raise ValueError(
            f"Storage implementation '{storage_name}' requires the following "
            f"environment variables: {', '.join(missing_vars)}"
        )


def linear_gradient_weighted_polling(
    entities_or_relations: list[dict],
    max_related_chunks: int,
    min_related_chunks: int = 1,
) -> list[str]:
    """
    Linear gradient weighted polling algorithm for text chunk selection.

    This algorithm ensures that entities/relations with higher importance get more text chunks,
    forming a linear decreasing allocation pattern.

    Args:
        entities_or_relations: List of entities or relations sorted by importance (high to low)
        max_related_chunks: Expected number of text chunks for the highest importance entity/relation
        min_related_chunks: Expected number of text chunks for the lowest importance entity/relation

    Returns:
        List of selected text chunk IDs
    """
    if not entities_or_relations:
        return []

    n = len(entities_or_relations)
    if n == 1:
        # Only one entity/relation, return its first max_related_chunks text chunks
        entity_chunks = entities_or_relations[0].get("sorted_chunks", [])
        return entity_chunks[:max_related_chunks]

    # Calculate expected text chunk count for each position (linear decrease)
    expected_counts = []
    for i in range(n):
        # Linear interpolation: from max_related_chunks to min_related_chunks
        ratio = i / (n - 1) if n > 1 else 0
        expected = max_related_chunks - ratio * (
            max_related_chunks - min_related_chunks
        )
        expected_counts.append(int(round(expected)))

    # First round allocation: allocate by expected values
    selected_chunks = []
    used_counts = []  # Track number of chunks used by each entity
    total_remaining = 0  # Accumulate remaining quotas

    for i, entity_rel in enumerate(entities_or_relations):
        entity_chunks = entity_rel.get("sorted_chunks", [])
        expected = expected_counts[i]

        # Actual allocatable count
        actual = min(expected, len(entity_chunks))
        selected_chunks.extend(entity_chunks[:actual])
        used_counts.append(actual)

        # Accumulate remaining quota
        remaining = expected - actual
        if remaining > 0:
            total_remaining += remaining

    # Second round allocation: multi-round scanning to allocate remaining quotas
    for _ in range(total_remaining):
        allocated = False

        # Scan entities one by one, allocate one chunk when finding unused chunks
        for i, entity_rel in enumerate(entities_or_relations):
            entity_chunks = entity_rel.get("sorted_chunks", [])

            # Check if there are still unused chunks
            if used_counts[i] < len(entity_chunks):
                # Allocate one chunk
                selected_chunks.append(entity_chunks[used_counts[i]])
                used_counts[i] += 1
                allocated = True
                break

        # If no chunks were allocated in this round, all entities are exhausted
        if not allocated:
            break

    return selected_chunks


class TokenTracker:
    """Track token usage for LLM calls."""

    def __init__(self):
        self.reset()

    def __enter__(self):
        self.reset()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        print(self)

    def reset(self):
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.call_count = 0

    def add_usage(self, token_counts):
        """Add token usage from one LLM call.

        Args:
            token_counts: A dictionary containing prompt_tokens, completion_tokens, total_tokens
        """
        self.prompt_tokens += token_counts.get("prompt_tokens", 0)
        self.completion_tokens += token_counts.get("completion_tokens", 0)

        # If total_tokens is provided, use it directly; otherwise calculate the sum
        if "total_tokens" in token_counts:
            self.total_tokens += token_counts["total_tokens"]
        else:
            self.total_tokens += token_counts.get(
                "prompt_tokens", 0
            ) + token_counts.get("completion_tokens", 0)

        self.call_count += 1

    def get_usage(self):
        """Get current usage statistics."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "call_count": self.call_count,
        }

    def __str__(self):
        usage = self.get_usage()
        return (
            f"LLM call count: {usage['call_count']}, "
            f"Prompt tokens: {usage['prompt_tokens']}, "
            f"Completion tokens: {usage['completion_tokens']}, "
            f"Total tokens: {usage['total_tokens']}"
        )


async def apply_rerank_if_enabled(
    query: str,
    retrieved_docs: list[dict],
    global_config: dict,
    enable_rerank: bool = True,
    top_n: int = None,
) -> list[dict]:
    """
    Apply reranking to retrieved documents if rerank is enabled.

    Args:
        query: The search query
        retrieved_docs: List of retrieved documents
        global_config: Global configuration containing rerank settings
        enable_rerank: Whether to enable reranking from query parameter
        top_n: Number of top documents to return after reranking

    Returns:
        Reranked documents if rerank is enabled, otherwise original documents
    """
    if not enable_rerank or not retrieved_docs:
        return retrieved_docs

    rerank_func = global_config.get("rerank_model_func")
    if not rerank_func:
        logger.warning(
            "Rerank is enabled but no rerank model is configured. Please set up a rerank model or set enable_rerank=False in query parameters."
        )
        return retrieved_docs

    try:
        # Apply reranking - let rerank_model_func handle top_k internally
        reranked_docs = await rerank_func(
            query=query,
            documents=retrieved_docs,
            top_n=top_n,
        )
        if reranked_docs and len(reranked_docs) > 0:
            if len(reranked_docs) > top_n:
                reranked_docs = reranked_docs[:top_n]
            logger.info(f"Successfully reranked: {len(retrieved_docs)} chunks")
            return reranked_docs
        else:
            logger.warning("Rerank returned empty results, using original chunks")
            return retrieved_docs

    except Exception as e:
        logger.error(f"Error during reranking: {e}, using original chunks")
        return retrieved_docs


async def process_chunks_unified(
    query: str,
    unique_chunks: list[dict],
    query_param: "QueryParam",
    global_config: dict,
    source_type: str = "mixed",
    chunk_token_limit: int = None,  # Add parameter for dynamic token limit
) -> list[dict]:
    """
    Unified processing for text chunks: deduplication, chunk_top_k limiting, reranking, and token truncation.

    Args:
        query: Search query for reranking
        chunks: List of text chunks to process
        query_param: Query parameters containing configuration
        global_config: Global configuration dictionary
        source_type: Source type for logging ("vector", "entity", "relationship", "mixed")
        chunk_token_limit: Dynamic token limit for chunks (if None, uses default)

    Returns:
        Processed and filtered list of text chunks
    """
    if not unique_chunks:
        return []

    origin_count = len(unique_chunks)

    # 1. Apply reranking if enabled and query is provided
    if query_param.enable_rerank and query and unique_chunks:
        rerank_top_k = query_param.chunk_top_k or len(unique_chunks)
        unique_chunks = await apply_rerank_if_enabled(
            query=query,
            retrieved_docs=unique_chunks,
            global_config=global_config,
            enable_rerank=query_param.enable_rerank,
            top_n=rerank_top_k,
        )

    # 2. Filter by minimum rerank score if reranking is enabled
    if query_param.enable_rerank and unique_chunks:
        min_rerank_score = global_config.get("min_rerank_score", 0.5)
        if min_rerank_score > 0.0:
            original_count = len(unique_chunks)

            # Filter chunks with score below threshold
            filtered_chunks = []
            for chunk in unique_chunks:
                rerank_score = chunk.get(
                    "rerank_score", 1.0
                )  # Default to 1.0 if no score
                if rerank_score >= min_rerank_score:
                    filtered_chunks.append(chunk)

            unique_chunks = filtered_chunks
            filtered_count = original_count - len(unique_chunks)

            if filtered_count > 0:
                logger.info(
                    f"Rerank filtering: {len(unique_chunks)} chunks remained (min rerank score: {min_rerank_score})"
                )
            if not unique_chunks:
                return []

    # 3. Apply chunk_top_k limiting if specified
    if query_param.chunk_top_k is not None and query_param.chunk_top_k > 0:
        if len(unique_chunks) > query_param.chunk_top_k:
            unique_chunks = unique_chunks[: query_param.chunk_top_k]
        logger.debug(
            f"Kept chunk_top-k: {len(unique_chunks)} chunks (deduplicated original: {origin_count})"
        )

    # 4. Token-based final truncation
    tokenizer = global_config.get("tokenizer")
    if tokenizer and unique_chunks:
        # Set default chunk_token_limit if not provided
        if chunk_token_limit is None:
            # Get default from query_param or global_config
            chunk_token_limit = getattr(
                query_param,
                "max_total_tokens",
                global_config.get("MAX_TOTAL_TOKENS", DEFAULT_MAX_TOTAL_TOKENS),
            )

        original_count = len(unique_chunks)
        unique_chunks = truncate_list_by_token_size(
            unique_chunks,
            key=lambda x: x.get("content", ""),
            max_token_size=chunk_token_limit,
            tokenizer=tokenizer,
        )
        logger.debug(
            f"Token truncation: {len(unique_chunks)} chunks from {original_count} "
            f"(chunk available tokens: {chunk_token_limit}, source: {source_type})"
        )

    return unique_chunks


def build_file_path(already_file_paths, data_list, target):
    """Build file path string with length limit and deduplication

    Args:
        already_file_paths: List of existing file paths
        data_list: List of data items containing file_path
        target: Target name for logging warnings

    Returns:
        str: Combined file paths separated by GRAPH_FIELD_SEP
    """
    # set: deduplication
    file_paths_set = {fp for fp in already_file_paths if fp}

    # string: filter empty value and keep file order in already_file_paths
    file_paths = GRAPH_FIELD_SEP.join(fp for fp in already_file_paths if fp)
    # ignored file_paths
    file_paths_ignore = ""
    # add file_paths
    for dp in data_list:
        cur_file_path = dp.get("file_path")
        # empty
        if not cur_file_path:
            continue

        # skip duplicate item
        if cur_file_path in file_paths_set:
            continue
        # add
        file_paths_set.add(cur_file_path)

        # check the length
        if (
            len(file_paths) + len(GRAPH_FIELD_SEP + cur_file_path)
            < DEFAULT_MAX_FILE_PATH_LENGTH
        ):
            # append
            file_paths += (
                GRAPH_FIELD_SEP + cur_file_path if file_paths else cur_file_path
            )
        else:
            # ignore
            file_paths_ignore += GRAPH_FIELD_SEP + cur_file_path

    if file_paths_ignore:
        logger.warning(
            f"Length of file_path exceeds {target}, ignoring new file: {file_paths_ignore}"
        )
    return file_paths


def generate_track_id(prefix: str = "upload") -> str:
    """Generate a unique tracking ID with timestamp and UUID

    Args:
        prefix: Prefix for the track ID (e.g., 'upload', 'insert')

    Returns:
        str: Unique tracking ID in format: {prefix}_{timestamp}_{uuid}
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    unique_id = str(uuid.uuid4())[:8]  # Use first 8 characters of UUID
    return f"{prefix}_{timestamp}_{unique_id}"
