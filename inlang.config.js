export async function defineConfig(env) {
	const { default: i18nextPlugin } = await env.$import(
		"https://cdn.jsdelivr.net/npm/@inlang/plugin-i18next@3/dist/index.js",
	)

	const { default: standardLintRules } = await env.$import(
		'https://cdn.jsdelivr.net/gh/inlang/standard-lint-rules@2/dist/index.js'
	);

	return {
		referenceLanguage: 'en',
		plugins: [i18nextPlugin({
			pathPattern: "./custom_components/better_thermostat/translations/{language}.json",
			variableReferencePattern: ["{", "}"]
		}), standardLintRules()]
	};
}