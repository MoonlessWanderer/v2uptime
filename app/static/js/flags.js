/*
 * Flag emoji check. Windows has no flag glyphs, so Chrome and Edge there draw "DE"
 * instead of the German flag. When that happens, add .flags-polyfill to <html>; the
 * stylesheet then puts the bundled "Twemoji Country Flags" font first (it covers only
 * the flag code points, so other text is unchanged and other browsers never fetch it).
 *
 * Method from talkjs/country-flag-emoji-polyfill (MIT): a colour emoji glyph ignores
 * fillStyle, letters do not. If a smiley draws in colour but the Swiss flag does not,
 * the browser lacks flag glyphs.
 */
(function () {
  "use strict";

  var EMOJI_FONTS = '"Twemoji Mozilla","Apple Color Emoji","Segoe UI Emoji","Segoe UI Symbol",' +
    '"Noto Color Emoji","EmojiOne Color","Android Emoji",sans-serif';

  function pixel(ctx, text, color) {
    ctx.clearRect(0, 0, 100, 100);
    ctx.fillStyle = color;
    ctx.fillText(text, 0, 0);
    return ctx.getImageData(0, 0, 1, 1).data.join(",");
  }

  function drawsInColour(text) {
    var canvas = document.createElement("canvas");
    canvas.width = canvas.height = 1;
    var ctx = canvas.getContext("2d", { willReadFrequently: true });
    ctx.textBaseline = "top";
    ctx.font = "100px " + EMOJI_FONTS;
    ctx.scale(0.01, 0.01);
    var onWhite = pixel(ctx, text, "#fff");
    var onBlack = pixel(ctx, text, "#000");
    return onWhite === onBlack && onBlack.indexOf("0,0,0,") !== 0;
  }

  try {
    if (drawsInColour("😊") && !drawsInColour("🇨🇭")) {
      document.documentElement.classList.add("flags-polyfill");
    }
  } catch (e) { /* no canvas: keep the system fonts */ }
})();
