"""Image tool results must not reach text-only GLM models, including continued chats."""

import copy
import unittest

from open_webui.utils.image_input import (
    IMAGE_OMITTED_NOTE,
    apply_image_input_support,
    model_reads_images,
    strip_image_inputs,
)


class ImageInputTest(unittest.TestCase):
    def test_glm_without_modality_metadata_is_text_only(self):
        for model_id in (
            'glm-5.3',
            'glm-5.2',
            'GLM-5.3',
            'z-ai/glm-5.3',
            'account:zai:glm-5.3',
            'account:openrouter:z-ai/glm-5.3',
            'connection.glm-5.3',
        ):
            with self.subTest(model_id=model_id):
                model = {'id': model_id}
                self.assertFalse(model_reads_images(model))
                apply_image_input_support([model])
                self.assertFalse(model['reads_images'])

    def test_account_and_connection_use_upstream_id(self):
        for model in (
            {'id': 'account:zai:glm-5.3', 'openai': {'id': 'glm-5.3'}},
            {'id': 'custom-id', 'upstream_model_id': 'glm-5.3'},
            {'id': 'connection.glm-5.3', 'openai': {'id': 'connection.glm-5.3'}},
        ):
            with self.subTest(model=model):
                self.assertFalse(model_reads_images(model))
                apply_image_input_support([model])
                self.assertFalse(model['reads_images'])

    def test_vision_variants_and_unknown_models_keep_image_support(self):
        for model_id in (
            'glm-5.3-flash',
            'glm-5.3-flashx',
            'account:zai:glm-5.3-flash',
            'glm-5v-turbo',
            'glm-4.6v',
            'glm-5.30',
            'glm-6',
            'custom-model',
        ):
            with self.subTest(model_id=model_id):
                model = {'id': model_id}
                self.assertTrue(model_reads_images(model))
                apply_image_input_support([model])
                self.assertTrue(model['reads_images'])

    def test_provider_metadata_takes_precedence_over_fallback(self):
        for details in (
            {'input_modalities': ['text', 'image']},
            {'architecture': {'input_modalities': ['text', 'image']}},
            {'modalities': {'input': ['text', 'image']}},
            {'architecture': {'modality': 'text+image->text'}},
            {'supports_vision': True},
        ):
            with self.subTest(details=details):
                model = {'id': 'account:zai:glm-5.3', 'openai': {'id': 'glm-5.3', **details}}
                self.assertTrue(model_reads_images(model))
                apply_image_input_support([model])
                self.assertTrue(model['reads_images'])

    def test_vision_switch_cannot_enable_a_text_only_model(self):
        model = {'id': 'glm-5.3', 'info': {'meta': {'capabilities': {'vision': True}}}}
        self.assertFalse(model_reads_images(model))
        apply_image_input_support([model])
        self.assertFalse(model['reads_images'])

        model = {'id': 'glm-5.3-flash', 'info': {'meta': {'capabilities': {'vision': False}}}}
        self.assertFalse(model_reads_images(model))
        apply_image_input_support([model])
        self.assertFalse(model['reads_images'])

    def test_presets_inherit_text_only_base_model(self):
        base = {'id': 'account:zai:glm-5.3', 'openai': {'id': 'glm-5.3'}}
        preset = {'id': 'coder', 'info': {'base_model_id': base['id']}}
        nested = {'id': 'my-coder', 'info': {'base_model_id': preset['id']}}
        apply_image_input_support([nested, preset, base])
        for model in (base, preset, nested):
            self.assertFalse(model_reads_images(model))

    def test_image_read_tool_continuation_becomes_text_only(self):
        # The native tool loop moves read_file's image into a user message. The same
        # guard runs when saved output is replayed by Continue or a new user turn.
        payload = {
            'messages': [
                {'role': 'user', 'content': 'Read hammer-c-light.png'},
                {
                    'role': 'assistant',
                    'content': '',
                    'tool_calls': [
                        {
                            'id': 'call-image',
                            'type': 'function',
                            'function': {'name': 'read_file', 'arguments': '{"path":"hammer-c-light.png"}'},
                        }
                    ],
                },
                {'role': 'tool', 'tool_call_id': 'call-image', 'content': 'read_file: Image file read successfully.'},
                {
                    'role': 'user',
                    'content': [
                        {
                            'type': 'text',
                            'text': 'Here are the images from the tool results above. Please analyze them.',
                        },
                        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,dGVzdA=='}},
                        {'type': 'image_url', 'image_url': {'url': '/api/v1/files/image/content'}},
                    ],
                },
            ],
        }
        original = copy.deepcopy(payload)
        model = {'id': 'account:zai:glm-5.3', 'openai': {'id': 'glm-5.3'}}
        apply_image_input_support([model])
        if not model_reads_images(model):
            strip_image_inputs(payload)

        self.assertEqual(payload['messages'][:3], original['messages'][:3])
        content = payload['messages'][-1]['content']
        self.assertEqual([part['type'] for part in content], ['text', 'text'])
        self.assertEqual(content[-1]['text'], IMAGE_OMITTED_NOTE)
        self.assertNotIn('base64', str(payload))
        # Retrying a continued chat neither reintroduces images nor duplicates the note.
        self.assertEqual(strip_image_inputs(copy.deepcopy(payload)), payload)

    def test_responses_tool_images_and_history_are_stripped(self):
        payload = {
            'input': [
                {'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'https://example.com/image.png'}]},
                {'type': 'function_call', 'call_id': 'read', 'name': 'read_file', 'arguments': '{}'},
                {
                    'type': 'function_call_output',
                    'call_id': 'read',
                    'output': [
                        {'type': 'input_text', 'text': 'Image read successfully.'},
                        {'type': 'input_image', 'image_url': 'data:image/png;base64,dGVzdA=='},
                    ],
                },
            ],
        }
        strip_image_inputs(payload)
        self.assertEqual(payload['input'][0]['content'], [{'type': 'input_text', 'text': IMAGE_OMITTED_NOTE}])
        self.assertEqual(payload['input'][1]['name'], 'read_file')
        self.assertEqual(payload['input'][2]['call_id'], 'read')
        self.assertEqual(
            payload['input'][2]['output'],
            [
                {'type': 'input_text', 'text': 'Image read successfully.'},
                {'type': 'input_text', 'text': IMAGE_OMITTED_NOTE},
            ],
        )


if __name__ == '__main__':
    unittest.main()
