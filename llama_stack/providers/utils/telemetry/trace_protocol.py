# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the terms described in the LICENSE file in
# the root directory of this source tree.

import asyncio
import inspect
from datetime import datetime
from functools import wraps
from typing import Any, AsyncGenerator, Callable, Type, TypeVar
from uuid import UUID

from pydantic import BaseModel

T = TypeVar("T")


def serialize_value(value: Any) -> Any:
    """Serialize a single value into JSON-compatible format."""
    if value is None:
        return None
    elif isinstance(value, (str, int, float, bool)):
        return value
    elif isinstance(value, BaseModel):
        return value.model_dump()
    elif isinstance(value, (list, tuple, set)):
        return [serialize_value(item) for item in value]
    elif isinstance(value, dict):
        return {str(k): serialize_value(v) for k, v in value.items()}
    elif isinstance(value, (datetime, UUID)):
        return str(value)
    else:
        return str(value)


def trace_protocol(cls: Type[T]) -> Type[T]:
    """
    A class decorator that automatically traces all methods in a protocol/base class
    and its inheriting classes.
    """

    def trace_method(method: Callable) -> Callable:
        from llama_stack.providers.utils.telemetry import tracing

        is_async = asyncio.iscoroutinefunction(method)
        is_async_gen = inspect.isasyncgenfunction(method)

        def create_span_context(self: Any, *args: Any, **kwargs: Any) -> tuple:
            class_name = self.__class__.__name__
            method_name = method.__name__
            span_type = (
                "async_generator" if is_async_gen else "async" if is_async else "sync"
            )
            sig = inspect.signature(method)
            param_names = list(sig.parameters.keys())[1:]  # Skip 'self'
            combined_args = {}
            for i, arg in enumerate(args):
                param_name = (
                    param_names[i] if i < len(param_names) else f"position_{i+1}"
                )
                combined_args[param_name] = serialize_value(arg)
            for k, v in kwargs.items():
                combined_args[str(k)] = serialize_value(v)

            span_attributes = {
                "__autotraced__": True,
                "__class__": class_name,
                "__method__": method_name,
                "__type__": span_type,
                "__args__": str(combined_args),
            }

            return class_name, method_name, span_attributes

        @wraps(method)
        async def async_gen_wrapper(
            self: Any, *args: Any, **kwargs: Any
        ) -> AsyncGenerator:
            class_name, method_name, span_attributes = create_span_context(
                self, *args, **kwargs
            )

            with tracing.span(f"{class_name}.{method_name}", span_attributes) as span:
                try:
                    count = 0
                    async for item in method(self, *args, **kwargs):
                        yield item
                        count += 1
                finally:
                    span.set_attribute("chunk_count", count)

        @wraps(method)
        async def async_wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            class_name, method_name, span_attributes = create_span_context(
                self, *args, **kwargs
            )

            with tracing.span(f"{class_name}.{method_name}", span_attributes) as span:
                try:
                    result = await method(self, *args, **kwargs)
                    span.set_attribute("output", serialize_value(result))
                    return result
                except Exception as e:
                    span.set_attribute("error", str(e))
                    raise

        @wraps(method)
        def sync_wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            class_name, method_name, span_attributes = create_span_context(
                self, *args, **kwargs
            )

            with tracing.span(f"{class_name}.{method_name}", span_attributes) as span:
                try:
                    result = method(self, *args, **kwargs)
                    span.set_attribute("output", serialize_value(result))
                    return result
                except Exception as _e:
                    raise

        if is_async_gen:
            return async_gen_wrapper
        elif is_async:
            return async_wrapper
        else:
            return sync_wrapper

    original_init_subclass = getattr(cls, "__init_subclass__", None)

    def __init_subclass__(cls_child, **kwargs):  # noqa: N807
        if original_init_subclass:
            original_init_subclass(**kwargs)

        for name, method in vars(cls_child).items():
            if inspect.isfunction(method) and not name.startswith("_"):
                setattr(cls_child, name, trace_method(method))  # noqa: B010

    cls.__init_subclass__ = classmethod(__init_subclass__)

    return cls