[⭐️Please star to support this work⭐️](https://github.com/vtbag/utensil-drawer)

# 🛠 The Utensil Drawer

Utensil Drawer: Pick the tools you need to craft the view transitions you want!

![Build Status](https://github.com/vtbag/utensil-drawer/actions/workflows/run-build.yml/badge.svg)
[![npm version](https://img.shields.io/npm/v/@vtbag/utensil-drawer/latest)](https://www.npmjs.com/package/@vtbag/utensil-drawer)
![minzip](https://badgen.net/bundlephobia/minzip/@vtbag/utensil-drawer)
[![NPM Downloads](https://img.shields.io/npm/dw/@vtbag/utensil-drawer)](https://www.npmjs.com/package/@vtbag/utensil-drawer)

The @vtbag website can be found at https://vtbag.dev/

## !!! News !!!

> `mayStartViewTransition` can now also handle scoped view transitions on supporting browsers. Usage: add `scope: someElement` to the extension object. 

For details see https://vtbag.dev/tools/utensil-drawer/

## What happened before?


> Do many of the elements you want to automatically add view-transition-names to fall outside the viewport? The declarative-names script now supports a new pseudo-class that you can add at the end of selectors. By using `:in-viewport`, only the elements overlapping with the current viewport will be named!


> mayStartViewTransition() gets better and better. Not only overhauled, refactored, optimized and thoroughly tested...

> ...now also supports an option that **rewrites view transition types** as CSS classes added to the :root element! In combination with the `postcss-active-view-transition-type` PostCSS plugin, you can use view transition types from Level 2 of the View Transition API even in browsers that only support Level 1 view transitions, yet. Looking at you, Firefox (Nightly). 

> **Access morph animation parameters directly in CSS!**
You can now access the key parameters of each morph animation in CSS rules! Make them available as CSS pseudo properties on your `::view-transition-group` elements. Calculate animation values based on old and new positions, widths, and heights. Let the `vectors` script handle the JavaScript while you create pure CSS styles that go far beyond basic morphs!


> Tiered of checking if `startViewTransition` is supported and whether it wants a function or also accepts the new object with view transitions types? The Drawer now includes the (still experimental) `mayStatViewTransition` function:
    * Works with the new signature in all supported browsers
    * Falls back gracefully if view transitions are not  natively supported
    * 🥁🥁🥁 Optionally **prevents killing** the current transitions when a new one is started **by automatically chaining** view transitions 🥁🥁🥁


> `escapeViewTransitionName()` is a function that escapes your view transition names so you are not stuck with just `A-Za-Z0-9-_` characters. "😀"! It's a handy alternative to `CSS.escape()` for environments where that's not available.

> Stable: `declarative-names` allows you to assign view transition names to a set of HTML elements, offering a more reliable and controllable alternative to `view-transition-name: auto` that works cross-browser and also for cross-document navigation.

For details see the [CHANGELOG](https://github.com/vtbag/utensil-drawer/blob/main/CHANGELOG.md)

## What is it?

The Utensil Drawer holds reusable functions to help you build websites with view transitions. It is a bit sparse right now, but like the one in your kitchen, it is bound to fill up over time.

