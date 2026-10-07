import io
import os
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from review.services.ai_provider import DEFAULT_ANTHROPIC_MODEL, anthropic_model


AI_KEYS = (
    'AI_PROVIDER', 'OLLAMA_MODEL', 'SHARED_QWEN_QUEUE_ENABLED', 'SHARED_QWEN_REDIS_URL', 'REDIS_URL',
    'ENABLE_CLOUD_FALLBACK', 'AI_PROVIDER_FALLBACK', 'ANTHROPIC_API_KEY', 'ANTHROPIC_MODEL',
    'AI_COST_ENFORCEMENT_ENABLED', 'AI_DAILY_COST_LIMIT_USD', 'AI_ANTHROPIC_INPUT_USD_PER_MILLION',
    'AI_ANTHROPIC_OUTPUT_USD_PER_MILLION', 'AI_MODEL_PRICING_JSON',
)

SHARED_QWEN = {
    'AI_PROVIDER': 'shared_qwen',
    'SHARED_QWEN_QUEUE_ENABLED': 'true',
    'SHARED_QWEN_REDIS_URL': 'redis://10.0.0.5:6379/0',
    'AI_COST_ENFORCEMENT_ENABLED': 'true',
    'AI_DAILY_COST_LIMIT_USD': '5',
}

CLOUD = {
    'ENABLE_CLOUD_FALLBACK': 'true',
    'ANTHROPIC_API_KEY': 'sk-ant-test-key',
    'ANTHROPIC_MODEL': 'claude-sonnet-5-5',
    'AI_ANTHROPIC_INPUT_USD_PER_MILLION': '3',
    'AI_ANTHROPIC_OUTPUT_USD_PER_MILLION': '15',
}


def run_checker(env):
    clean = {k: v for k, v in os.environ.items() if k not in AI_KEYS}
    clean.update(env)
    out = io.StringIO()
    with mock.patch.dict(os.environ, clean, clear=True):
        try:
            call_command('verify_production_readiness', stdout=out)
        except CommandError:
            pass
    return out.getvalue()


def line_for(output, text):
    for line in output.splitlines():
        if text in line:
            return line
    return ''


class ReviewModelDefaultTests(SimpleTestCase):
    def test_review_path_defaults_to_sonnet(self):
        with mock.patch.dict(os.environ, {'ANTHROPIC_MODEL': ''}):
            self.assertEqual(anthropic_model(), DEFAULT_ANTHROPIC_MODEL)
        self.assertIn('sonnet', DEFAULT_ANTHROPIC_MODEL)

    def test_explicit_model_is_respected(self):
        with mock.patch.dict(os.environ, {'ANTHROPIC_MODEL': 'claude-opus-5-5'}):
            self.assertEqual(anthropic_model(), 'claude-opus-5-5')


class SharedQwenReadinessTests(SimpleTestCase):
    def test_shared_qwen_is_an_accepted_provider(self):
        out = run_checker(SHARED_QWEN)
        self.assertTrue(line_for(out, 'AI_PROVIDER must be explicitly set').startswith('[PASS]'))
        self.assertTrue(line_for(out, 'SHARED_QWEN_QUEUE_ENABLED must be true').startswith('[PASS]'))
        self.assertTrue(line_for(out, 'SHARED_QWEN_REDIS_URL must be').startswith('[PASS]'))

    def test_unknown_provider_still_fails(self):
        out = run_checker({**SHARED_QWEN, 'AI_PROVIDER': 'auto'})
        self.assertTrue(line_for(out, 'AI_PROVIDER must be explicitly set').startswith('[FAIL]'))

    def test_queue_switched_off_fails(self):
        out = run_checker({**SHARED_QWEN, 'SHARED_QWEN_QUEUE_ENABLED': 'false'})
        self.assertTrue(line_for(out, 'SHARED_QWEN_QUEUE_ENABLED must be true').startswith('[FAIL]'))

    def test_missing_or_placeholder_queue_address_fails(self):
        for url in ('', 'CHANGE_ME_REDIS_URL', 'http://10.0.0.5:6379'):
            env = {**SHARED_QWEN, 'SHARED_QWEN_REDIS_URL': url}
            out = run_checker(env)
            self.assertTrue(line_for(out, 'SHARED_QWEN_REDIS_URL must be').startswith('[FAIL]'), url)

    def test_redis_url_is_accepted_as_the_queue_address(self):
        env = {**SHARED_QWEN, 'REDIS_URL': 'rediss://cache.internal:6380/0'}
        env.pop('SHARED_QWEN_REDIS_URL')
        out = run_checker(env)
        self.assertTrue(line_for(out, 'SHARED_QWEN_REDIS_URL must be').startswith('[PASS]'))

    def test_development_ollama_model_still_rejected(self):
        out = run_checker({**SHARED_QWEN, 'AI_PROVIDER': 'ollama', 'OLLAMA_MODEL': 'qwen2.5:0.5b-instruct'})
        self.assertTrue(line_for(out, 'qwen2.5:0.5b-instruct is a development model').startswith('[FAIL]'))


class CloudFallbackReadinessTests(SimpleTestCase):
    def test_cloud_fallback_needs_a_key_and_pricing(self):
        out = run_checker({**SHARED_QWEN, 'ENABLE_CLOUD_FALLBACK': 'true'})
        self.assertTrue(line_for(out, 'ANTHROPIC_API_KEY must be configured when ENABLE_CLOUD_FALLBACK').startswith('[FAIL]'))
        self.assertTrue(line_for(out, 'Anthropic input and output pricing').startswith('[FAIL]'))

    def test_complete_cloud_fallback_passes(self):
        out = run_checker({**SHARED_QWEN, **CLOUD})
        self.assertTrue(line_for(out, 'ANTHROPIC_API_KEY must be configured').startswith('[PASS]'))
        self.assertTrue(line_for(out, 'Anthropic input and output pricing').startswith('[PASS]'))
        self.assertNotIn('[WARN]', out)

    def test_no_anthropic_checks_without_cloud_use(self):
        out = run_checker(SHARED_QWEN)
        self.assertEqual(line_for(out, 'ANTHROPIC_API_KEY'), '')

    def test_haiku_review_model_warns(self):
        out = run_checker({**SHARED_QWEN, **CLOUD, 'ANTHROPIC_MODEL': 'claude-haiku-4-5-20251001'})
        self.assertIn('[WARN] ANTHROPIC_MODEL is claude-haiku-4-5-20251001', out)

    def test_unused_fallback_setting_warns(self):
        out = run_checker({**SHARED_QWEN, 'AI_PROVIDER_FALLBACK': 'anthropic'})
        self.assertIn('[WARN] AI_PROVIDER_FALLBACK is not read by the application', out)


class TemplateTests(SimpleTestCase):
    def _template(self, name):
        from django.conf import settings
        path = settings.BASE_DIR / name
        if not path.exists():
            self.skipTest(f'{name} is not in the repository')
        with open(path, encoding='utf-8') as fh:
            return {
                k.strip(): v.strip()
                for k, _, v in (line.partition('=') for line in fh if '=' in line and not line.startswith('#'))
            }

    def test_dev_template_uses_sonnet_for_review(self):
        self.assertIn('sonnet', self._template('.env.example')['ANTHROPIC_MODEL'])

    def test_production_template_uses_sonnet_for_review(self):
        self.assertIn('sonnet', self._template('.env.production.example')['ANTHROPIC_MODEL'])

    def test_production_template_uses_the_real_fallback_setting(self):
        values = self._template('.env.production.example')
        self.assertNotIn('AI_PROVIDER_FALLBACK', values)
        self.assertEqual(values['ENABLE_CLOUD_FALLBACK'], 'true')
        self.assertEqual(values['AI_PROVIDER'], 'shared_qwen')
