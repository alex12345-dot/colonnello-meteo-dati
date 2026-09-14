import numpy as np

from pipeline.encode import dequantize, encode_scalar, encode_wind, read_png


def test_scalar_png_round_trip_within_declared_quantization(fields, tmp_path):
    original = fields["2t"].values
    path = tmp_path / "temperature.png"
    metadata = encode_scalar(original, path)
    pixels = read_png(path)
    decoded = dequantize(pixels, metadata["min"], metadata["max"])
    assert pixels.shape == (721, 1440)
    assert np.max(np.abs(decoded - original)) <= metadata["quantization_step"]


def test_wind_png_uses_red_green_and_alpha(fields, tmp_path):
    path = tmp_path / "wind.png"
    metadata = encode_wind(fields["10u"].values, fields["10v"].values, path)
    pixels = read_png(path)
    assert pixels.shape == (721, 1440, 4)
    assert np.all(pixels[..., 2] == 0)
    assert np.all(pixels[..., 3] == 255)
    for channel, source in enumerate((fields["10u"].values, fields["10v"].values)):
        decoded = dequantize(
            pixels[..., channel], metadata["min"][channel], metadata["max"][channel]
        )
        assert np.max(np.abs(decoded - source)) <= metadata["quantization_step"][channel]

