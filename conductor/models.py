DEFAULT_MODEL = 'moonshotai/kimi-k3.1-pro'


def supports_native_images(model):
    # These model families expose image input through OpenRouter. Other models
    # retain structured accessibility observations and local screenshot artifacts.
    return model in {'moonshotai/kimi-k3.1-pro', 'moonshotai/kimi-k3',
                     'moonshotai/kimi-k2.7-code', 'moonshotai/kimi-k2.6', 'moonshotai/kimi-k2.5'}
