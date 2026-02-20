[⭐️Please star to support this work⭐️](https://github.com/vtbag/cam-shaft)

# 🐫 The Cam-Shaft

Cam-Shaft: Bump your view transition pseudo-elements into place to avoid that unexpected pseudo-smooth-scrolling effect.

![Build Status](https://github.com/vtbag/cam-shaft/actions/workflows/run-build.yml/badge.svg)
[![npm version](https://img.shields.io/npm/v/@vtbag/cam-shaft/latest)](https://www.npmjs.com/package/@vtbag/cam-shaft)
![minzip](https://badgen.net/bundlephobia/minzip/@vtbag/cam-shaft)
[![NPM Downloads](https://img.shields.io/npm/dw/@vtbag/cam-shaft)](https://www.npmjs.com/package/@vtbag/cam-shaft)

The @vtbag website can be found at https://vtbag.dev/

## !!! News !!!

The Cam-Shaft should now also work for browsers that support cross-document view transitions but have no support for the Navigation API (looking at you, Safari).

For details see the [CHANGELOG](https://github.com/vtbag/cam-shaft/blob/main/CHANGELOG.md)


## What is it?

When you assign a `view-transition-name` to an element that is larger than the viewport, the View Transition API adds a default animation to the `::view-transition-group()` of the element that makes instant scrolling look like smooth scrolling. The Cam-Shaft bumps and nudges your view transition pseudo-elements back into place to avoid that pseudo-smooth-scrolling effect.

[See the Cam-Shaft in action](https://vtbag.dev/shaft-demo2/1/) and [see how it can be used in your own projects](https://vtbag.dev/tools/cam-shaft/).