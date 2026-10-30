"""Retired Mem0 transport, retained only for historical audit.

The user explicitly prohibited Mem0 from using the project's model endpoints.
All model and SDK entry points fail closed, without an environment override.
"""
from contextvars import ContextVar
from dataclasses import dataclass

_binding = ContextVar('mem0_comparison_binding', default=None)


class Mem0ExecutionDisabled(RuntimeError):
    """Mem0 must not use the user's model or embedding endpoints."""


def prohibit_mem0_execution():
    raise Mem0ExecutionDisabled(
        'Mem0 execution is disabled by user instruction. '
        'Evaluate and improve memsys only; retain old Mem0 files for audit.')


@dataclass
class Binding:
    models: object
    settings: object
    tags: dict


class ChatBridge:
    def __init__(self, config=None):
        self.config = config
        self.binding = _binding.get()
        if self.binding is None:
            raise RuntimeError('Mem0 bridge requires an explicit model binding')

    def generate_response(self, messages, tools=None, tool_choice='auto', **kwargs):
        prohibit_mem0_execution()


class EmbeddingBridge:
    def __init__(self, config=None):
        self.config = config
        self.binding = _binding.get()
        if self.binding is None:
            raise RuntimeError('Mem0 bridge requires an explicit embedding binding')

    def embed(self,text,memory_action='add'):
        return self.embed_batch([text],memory_action)[0]

    def embed_batch(self,texts,memory_action='add'):
        prohibit_mem0_execution()


def memory_for(models,settings,root,question_id,dimension,*,state_root=None):
    prohibit_mem0_execution()


def close_memory(memory):
    memory.close()
    client=getattr(getattr(memory,'vector_store',None),'client',None)
    if client is not None and hasattr(client,'close'):
        client.close()
