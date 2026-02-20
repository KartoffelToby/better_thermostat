declare namespace App {
	type StarlightLocals = import('@astrojs/starlight').StarlightLocals;
	// Define the `locals.t` object in the context of a plugin.
	interface Locals extends StarlightLocals {}
}

declare namespace StarlightApp {
	// Define the additional plugin translations in the `I18n` interface.
	interface I18n {
		'myPlugin.doThing': string;
	}
}
