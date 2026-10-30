"""Remote rerank contract checks with synthetic responses; no real model inference."""
import io
import json
import unittest
from unittest.mock import patch
from memsys.config import MemorySettings
from memsys.reranking import RerankDocument, RerankFailure
from memsys.remote_rerank import RemoteReranker


class RerankTests(unittest.TestCase):
    def settings(self):
        return MemorySettings(rerank_backend="remote", rerank_url="https://placeholder.invalid")

    def test_remote_request_keeps_role_and_source_range(self):
        seen=[]
        def respond(req, **kwargs):
            seen.append(json.loads(req.data))
            return io.BytesIO(b'{"scores":{"E1":0.5},"trace":{"model":"synthetic"}}')
        with patch('urllib.request.urlopen',side_effect=respond):
            scores,trace=RemoteReranker(self.settings()).rank('q',[RerankDocument('E1','text',source_start=123,source_role='User')])
        self.assertEqual(scores,{'E1':.5})
        self.assertEqual(seen[0]['documents'][0]['source_start'],123)
        self.assertEqual(seen[0]['documents'][0]['source_role'],'User')
        self.assertEqual(trace['backend'],'remote')

    def test_incomplete_and_nonfinite_scores_rejected(self):
        for value in ({}, {'E1': float('nan')}, {'E1':True}):
            with self.subTest(value=value):
                with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps({'scores':value}).encode())):
                    with self.assertRaises(RerankFailure):
                        RemoteReranker(self.settings()).rank('q',[RerankDocument('E1','text')])

    def test_transport_failure_is_explicit(self):
        with patch('urllib.request.urlopen',side_effect=OSError('synthetic network failure')):
            with self.assertRaises(RerankFailure):
                RemoteReranker(self.settings()).rank('q',[RerankDocument('E1','text')])


if __name__=='__main__':
    unittest.main()
