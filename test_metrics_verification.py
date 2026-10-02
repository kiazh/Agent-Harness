"""Quick verification of metrics module."""
from ah.core.metrics import MetricsCollector, timed, JsonFormatter
import json

m = MetricsCollector()

# Test latency recording
m.record_latency('test.op', 10.0)
m.record_latency('test.op', 20.0)
m.record_latency('test.op', 30.0)
stats = m.get_latency_stats('test.op')
assert stats['count'] == 3
assert stats['min'] == 10.0
assert stats['max'] == 30.0
assert stats['mean'] == 20.0
print('✓ Latency stats:', stats)

# Test counters
m.increment_counter('test.op')
m.increment_counter('test.op')
assert m.get_counter('test.op') == 2
print('✓ Counter:', m.get_counter('test.op'))

# Test error tracking
m.record_error('test.op')
assert m.get_error_count('test.op') == 1
assert m.get_error_rate('test.op') == 0.5
print('✓ Error rate:', m.get_error_rate('test.op'))

# Test token usage
m.record_tokens('session-1', 100, 50)
m.record_tokens('session-1', 200, 100)
usage = m.get_token_usage('session-1')
assert usage['prompt_tokens'] == 300
assert usage['completion_tokens'] == 150
assert usage['total_tokens'] == 450
print('✓ Token usage:', usage)

# Test DB call recording
m.record_db_call('execute', 5.0)
m.record_db_call('fetch', 10.0, is_error=True)
db_stats = m.get_latency_stats('db.execute')
assert db_stats['count'] == 1
print('✓ DB stats:', db_stats)

# Test LLM call recording
m.record_llm_call('session-1', 'gpt-4', 500.0, 100, 50)
llm_stats = m.get_latency_stats('llm.gpt-4')
assert llm_stats['count'] == 1
print('✓ LLM stats:', llm_stats)

# Test all metrics
all_metrics = m.get_all_metrics()
assert 'latencies' in all_metrics
assert 'counters' in all_metrics
assert 'errors' in all_metrics
assert 'error_rates' in all_metrics
assert 'token_usage' in all_metrics
print('✓ All metrics keys present')

# Test reset
m.reset()
assert m.get_counter('test.op') == 0
print('✓ Reset works')

# Test timed context manager
with timed('timed.op'):
    pass
assert m.get_counter('timed.op.calls') == 1
print('✓ Timed context manager works')

# Test JsonFormatter
import logging
formatter = JsonFormatter()
record = logging.LogRecord('test', logging.INFO, 'test.py', 1, 'test message', (), None)
output = formatter.format(record)
data = json.loads(output)
assert data['message'] == 'test message'
assert data['level'] == 'INFO'
print('✓ JsonFormatter works')

print()
print('All metrics module tests passed!')
