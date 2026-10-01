from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pre_active.context import ContextAssembler
from pre_active.engine import Engine
from pre_active.providers.openai_compatible import OpenAICompatibleAdapter
from pre_active.store import Store
from pre_active.tools import ToolRegistry, ToolSpec


class ToolLoopHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        payload = json.loads(body.decode('utf-8'))
        type(self).requests.append(payload)
        messages = payload['messages']
        saw_result = any(
            'tool: math.double =>' in item.get('content', '')
            for item in messages
        )
        if saw_result:
            message = {'role': 'assistant', 'content': 'TOOL_LOOP_OK:12'}
        else:
            assert payload['tools'][0]['function']['name'] == 'math.double'
            message = {
                'role': 'assistant',
                'content': None,
                'tool_calls': [
                    {
                        'id': 'call-http-1',
                        'type': 'function',
                        'function': {
                            'name': 'math.double',
                            'arguments': '{"value":6}',
                        },
                    }
                ],
            }
        response = json.dumps({'choices': [{'message': message}]}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def test_engine_completes_structured_tool_loop_over_real_http(tmp_path: Path) -> None:
    ToolLoopHandler.requests = []
    server = ThreadingHTTPServer(('127.0.0.1', 0), ToolLoopHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        store = Store(tmp_path / 'state.db')
        tools = ToolRegistry(store)
        tools.register(
            ToolSpec(
                name='math.double',
                description='Double an integer',
                input_schema={
                    'type': 'object',
                    'properties': {'value': {'type': 'integer'}},
                    'required': ['value'],
                    'additionalProperties': False,
                },
                capability='math.double',
                mutation=False,
            ),
            lambda args: {'value': int(args['value']) * 2},
        )
        adapter = OpenAICompatibleAdapter(
            base_url=f'http://127.0.0.1:{server.server_port}/v1',
            model='integration-model',
            api_key=None,
            timeout_seconds=5.0,
        )
        engine = Engine(
            store=store,
            model=adapter,
            tools=tools,
            context=ContextAssembler(store),
            system_prompt='Use the provided tool, then finish.',
            worker_id='http-integration',
        )
        run_id = engine.submit_task('Double 6.', {'math.double'}, now=1.0)

        assert engine.run_once(now=2.0) == run_id
        assert store.get_run(run_id)['status'] == 'RUNNING'
        assert engine.run_once(now=3.0) == run_id

        run = store.get_run(run_id)
        assert run['status'] == 'COMPLETED'
        assert run['final_text'] == 'TOOL_LOOP_OK:12'
        assert run['last_error'] is None
        assert store.pending_event_count() == 0
        assert len(ToolLoopHandler.requests) == 2
        assert ToolLoopHandler.requests[0]['parallel_tool_calls'] is False
        assert any(
            'tool: math.double =>' in item.get('content', '')
            and '"value": 12' in item.get('content', '')
            for item in ToolLoopHandler.requests[1]['messages']
        )
        store.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)
