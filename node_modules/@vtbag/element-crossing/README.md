[⭐️Please star to support this work⭐️](https://github.com/vtbag/element-crossing)

# 🚸 The Element Crossing

Transfer selected element state across cross-document view transitions.

![Build Status](https://github.com/vtbag/element-crossing/actions/workflows/run-build.yml/badge.svg)
[![npm version](https://img.shields.io/npm/v/@vtbag/element-crossing/latest)](https://www.npmjs.com/package/@vtbag/element-crossing)
![minzip](https://badgen.net/bundlephobia/minzip/@vtbag/element-crossing)
[![NPM Downloads](https://img.shields.io/npm/dw/@vtbag/element-crossing)](https://www.npmjs.com/package/@vtbag/element-crossing)

The @vtbag website can be found at https://vtbag.dev/

## !!! News !!!

Native cross-document view transitions now work for Safari, and so does the Element Crossing!

For details see the [CHANGELOG](https://github.com/vtbag/element-crossing/blob/main/CHANGELOG.md)

## What happened before?

> Full support for crossing of `data-*` properties.

> Now also preserves the animation state of animated SVGs across cross-document view transitions, thanks to [Lukas](https://github.com/trombach)!

> Small improvements for smoother animations and handling reloads.


## What is it?

Boost the smoothness of your cross-document view transitions with the Element Crossing library! No more resetting to static states! This library preserves the current dynamic state of your DOM and CSS properties, ensuring a seamless user experience.

With Element Crossing, you can automatically retain:

- Current form inputs
- Current scrollbar positions
- Current state of active CSS animations
- Current media playback time
- Current toggle states
- Current values of dynamically added classes and CSS properties
- ...and (current) anything else you'd like to carry over from the previous page to the new one!

Simply annotate your elements in the HTML source or DOM, and let the library handle the rest. Keep your users engaged by preserving the exact state they left off, making transitions across documents smoother than ever!

Address any CSS property or DOM element property, any CSS class, or CSS animation.

[View configuration examples](https://vtbag.dev/tools/element-crossing/#applications-with-real-world-examples) and [see the Element Crossing in action](https://vtbag.dev/crossing/vanilla/1/)
