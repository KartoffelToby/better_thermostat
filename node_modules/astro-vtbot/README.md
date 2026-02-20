No, its **_NOT_** a roBOT 🤖, its a 👜 <b>B</b>ag <b>o</b>f <b>T</b>ricks!✨

[⭐️Please star to support this work⭐️](https://github.com/martrapp/astro-vtbot)

# **The Bag of Tricks** for Astro's **View Transitions**

The bag of tricks provides extensions & support around Astro's view transitions.

[![npm version](https://img.shields.io/npm/v/astro-vtbot/latest)](https://www.npmjs.com/package/astro-vtbot)
[![Socket Badge](https://socket.dev/api/badge/npm/package/astro-vtbot/)](https://socket.dev/npm/package/astro-vtbot/overview)
![Build Status](https://github.com/martrapp/astro-vtbot/actions/workflows/run-tests.yml/badge.svg)
[![NPM Downloads](https://img.shields.io/npm/dw/astro-vtbot)](https://www.npmjs.com/package/astro-vtbot)

A current deployment of tech demos and the documentation can be found at https://events-3bg.pages.dev/

## !!! NEW TRICKS ✨ IN THE BAG 👜 !!!

> Updated dependencies. Especially bumps @vtbag/utensil-drawer to current 1.2.13, where the `declarative-names` script now supports a special `:in-viewport` pseudo-class and `mayStartViewTransition()` supports scoped view transitions, if the browser does. For details see https://vtbag.dev/tools/utensil-drawer/

## Recently Learned Tricks ##

> Ever wanted a backward slide on that link to the home page?
The `BackLink` component flips the direction on selected links, giving you a backward animation on click, and a forward animation when navigating back through the browser's history.

> General fixes and improvements, see [CHANGELOG](https://github.com/martrapp/astro-vtbot/blob/main/CHANGELOG.md).

> Hello Astronauts! Now this one is BIG! The `astro-vtbot` has been many things so far. Now it is also a **Starlight plugin**! Add the Bag to your  [plugin list](https://events-3bg.pages.dev/library/StarlightPlugin/#installation) and instantaneously  turn on browser-native cross-document view transitions for your existing Starlight site![^1] Starlight version >= 0.32 required!

[^1]: On supporting browsers, which is all major browsers but Firefox, and we are looking forward to Firefox joining the team this year as well!


**Version 2.0.0 🎉 of astro-vtbot is here!**

The major release changes what is automatically installed if you install astro-vtbot as an Astro integration. Now installing as an integration using `astro add astro-vtbot` should be your default choice for installing astro-vtbot. This gives you access to the `inspection-chamber` on every page via Astro's devToolbar!

In contrast, `<LoadingIndicator />` and the `<Linter />` components will no longer be automatically integrated and must be explicitly added if needed, e.g. in a global Layout.astro. During a transition period, the previous behavior can still be restored using toggles in the astro.config file.

For details, see the [CHANGELOG](https://github.com/martrapp/astro-vtbot/blob/main/CHANGELOG.md).

**On the plus side:** There’s an exciting new mode in the `<TurnSignal />` component! It allows for view transition types on a per-link basis!
For further information see these demos: [blog demo](http://events-3bg.pages.dev/signal-demo/link-types/blog/), [@vtbag image viewer demo](https://vtbag.dev/viewer-demo/) and the [@vtbag fishpond demo](https://vtbag.dev/link-demo/).


> The integration setup can now also find the `astro-inspection-chamber.js.ts` file when used in monorepos with `pnpm`. \
Big shout out and "Thank You!" to [Luísa](https://github.com/luisaverza) for providing this fix!

> The Inspection Chamber DevTools now also work for `pnpm` projects! \
Big shout out and "Thank You!" to [Lukas](https://github.com/Trombach) for providing this fix!


## Reusable Components 🧩

- In need for extensions for view transitions because you have issues with iframes on your pages?
- Wanting support in understanding and debugging view transitions or simply want a second pair of eyes 👀 on your view transition settings?
- Looking for reusable animations or special transition effects?
- Want to use view transitions on your Starlight site?

The `astro-vtbot`package isn't a monolithic library. Use the components you need and only pay bandwidth for those.
|Component|Brotli bytes added|
|-------|-----------------|
Animation Style ✨| ~0.1k
AutoNameSelected 📛 | ~0.3k
BackLink 🔙 | ~0.1k
BorderControl 🛂 | ~0.1k
BrakePad 🦥 | ~0.2k
CamShaft 🐫 | ~0.6k
ElementCrossing 🚸 | ~1.2K
InspectionChamber 🔬 | ~27k
Linter 🧹 | ~1.9k
LoadingIndicator ⏳ | ~0.4k
Move 🚟 | ~0.2k
NoScroll 📜 | ~0.1k
PageOffset 📄⇞ | ~0.1k
PointerOnNavigation 👆 | ~0.1k
Portal 🚪 | ~0.2k
ReplacementSwap ↹ | ~0.5k
Starlight &hellip; 🌟 | ~3.0k
SwapSound 🔊 | -0.3k
Swing 🎷 | ~0.1k
TurnSignal 🚘 |~0.5k
VtBotDebug 🐛 | ~2.8k
Zoom 🔎 | ~0.1k

Visit [the documentation](https://events-3bg.pages.dev/components/) of the reusable components for detailed information.

- `<Linter/>`: A linter component that helps you identify problems when setting up transitions.

- `<VtBotDebug/>`: A Debugging component that logs the events and their data as they occur.

- `<ReplacementSwap/>`: An alterantive DOM swap(), which preserves elements in the original DOM to avoid reinitialization of iframes or CSS animations.

- `<LoadIndicator>`: Have you ever missed the visual feedback on sites with view transitions as to whether the app has noticed the click? You need a loading indicator! Here you go!

- `Zoom`, `<Move>` and `Swing` animations and the `<AnimationsStyle/>` component allows for extended styling options.

- `<Portal/>` component that forces all view transitions through a portal/loading page.

- `<NoScroll/>` keep the current vertical and horizontal scroll position when transitioning to the next page.

## Tech Demos 🔥

The bag of tricks currently contains [several technical demos](https://events-3bg.pages.dev/demos/) that show examples of the implementation of various effects using the view transition events.

The sources are in [this repository](https://github.com/martrapp/astro-vtbot-website).

## The Jotter 📓

Last but not least, the deployment also includes the [▶ Jotter ◀](https://events-3bg.pages.dev/jotter/) with a wealth of information on transition events as well as background information and valuable tips & tricks on view transitions in Astro.

Some of the contents are technical demos, some are useful tools, and some are reusable components that you can use in your own project to handle edge cases that go beyond Astro's standard features.

## Troubleshooting

For help, check out the `Discussions` tab on the [GitHub repo](https://github.com/martrapp/astro-vtbot/discussions).

## Contributing

This package is maintained by [martrapp](https://github.com/martrapp) independently from Astro. You're welcome to contribute by submitting an issue or opening a PR!

## Changelog

See [CHANGELOG.md](https://github.com/martrapp/astro-vtbot/blob/main/CHANGELOG.md) for a history of changes to this package.
