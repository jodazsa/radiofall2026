#!/usr/bin/env python3

import time

import board
import busio
import adafruit_ssd1306
from PIL import Image, ImageDraw, ImageFont


WIDTH = 128
HEIGHT = 32
ADDRESS = 0x3C


def main():
    print("Initializing I2C...")
    i2c = busio.I2C(board.SCL, board.SDA)

    print(f"Initializing SSD1306 at 0x{ADDRESS:02X}...")
    display = adafruit_ssd1306.SSD1306_I2C(
        WIDTH,
        HEIGHT,
        i2c,
        addr=ADDRESS,
    )

    image = Image.new("1", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    draw.text((0, 0), "RadioFall2026", font=font, fill=255)
    draw.text((0, 11), "OLED OK", font=font, fill=255)
    draw.text((0, 22), "I2C 0x3C", font=font, fill=255)

    display.image(image)
    display.show()

    print("OLED test image displayed for 5 seconds.")
    time.sleep(5)

    display.fill(0)
    display.show()

    print("OLED test passed.")


if __name__ == "__main__":
    main()