def test_emotion_style_hashed_classes_are_noise() -> None:
    # emotion / styled-components emit lowercase letter+digit INTERLEAVED classes with no separator
    # (`e1d6xluq5`, `ej9ium94`): a model that reads them off the skeleton writes brittle selectors,
    # so they are dropped as noise; semantic names with digits (`col-xs-6`, `h2`) are kept.
    from web.parse.classes import is_noise_class

    for tok in ("e1d6xluq5", "ej9ium94", "e4wm5bw1", "css-1a2b3c"):
        assert is_noise_class(tok), tok
    for tok in ("col-xs-6", "h2", "product_pod", "price_color", "ssrcss-evdvfk-StyledListItem"):
        assert not is_noise_class(tok), tok
