const decodeDiv = document.createElement('div');

export const ILLEGAL_TRANSITION_NAMES = 'data-vtbot-illegal-transition-names';

export function walkSheets(
	sheets: CSSStyleSheet[],
	withSheet?: (sheet: CSSStyleSheet) => void,
	withStyleRule?: (rule: CSSStyleRule) => void,
	afterSheet?: (sheet: CSSStyleSheet) => void
) {
	sheets.forEach((sheet) => {
		try {
			withSheet && withSheet(sheet);
			walkRules([...sheet.cssRules], withSheet, withStyleRule, afterSheet);
			afterSheet && afterSheet(sheet);
		} catch (e) {
			console.log(`%c[vtbot] Can't analyze sheet at ${sheet.href}: ${e}`, 'color: #888');
		}
	});
}

export function walkRules(
	rules: CSSRule[],
	withSheet?: (sheet: CSSStyleSheet) => void,
	withStyleRule?: (rule: CSSStyleRule) => void,
	afterSheet?: (sheet: CSSStyleSheet) => void
) {
	rules.forEach((rule) => {
		if (rule.constructor.name === 'CSSStyleRule') {
			withStyleRule && withStyleRule(rule as CSSStyleRule);
		} else if ('cssRules' in rule) {
			walkRules([...(rule.cssRules as CSSRuleList)], withSheet, withStyleRule, afterSheet);
		} else if ('styleSheet' in rule) {
			walkSheets([rule.styleSheet as CSSStyleSheet], withSheet, withStyleRule, afterSheet);
		}
	});
}

export function astroContextIds(doc = document) {
	const inStyleSheets = new Set<string>();
	const inElements = new Set<string>();

	walkSheets([...doc.styleSheets], undefined, (r) => {
		[...r.selectorText.matchAll(/data-astro-cid-(\w{8})/g)].forEach((match) =>
			inStyleSheets.add(match[1]!)
		);
		[...r.selectorText.matchAll(/\.astro-(\w{8})/g)].forEach((match) =>
			inStyleSheets.add(match[1]!)
		);
	});

	const ASTRO_CID = 'astroCid';
	[...doc.querySelectorAll('*')].forEach((el) => {
		Object.keys((el as HTMLElement).dataset).forEach((key) => {
			if (key.startsWith(ASTRO_CID)) {
				inElements.add(key.substring(ASTRO_CID.length).toLowerCase().replace(/^-/g, ''));
			}
		});
		el.classList.forEach((cls) => {
			if (cls.match(/^astro-(........)$/)) {
				inElements.add(cls.replace(/^astro-/, ''));
			}
		});
	});

	return { inStyleSheets, inElements };
}

type SupportedCSSProperties = 'view-transition-name';
// finds all elements of _an active_ document with a given _string_ property in a style sheet.
// document.styleSheets does not work for documents that are not associated with a window
export function elementsWithPropertyInStylesheet(
	doc: Document,
	property: SupportedCSSProperties,
	map: Map<string, Set<Element>> = new Map()
): Map<string, Set<Element>> {
	const definitions = new Map<CSSStyleSheet, Set<string>>();

	walkSheets([...doc.styleSheets], (sheet) => {
		const owner = sheet.ownerNode;
		if (definitions.has(sheet)) return;
		const set = new Set<string>();
		definitions.set(sheet, set);
		const text = (owner?.textContent ?? '').replace(/@supports[^;{]+/g, '');
		const matches = text.matchAll(new RegExp(`${property}:\\s*([^;}]*)`, 'gu'));
		[...matches].forEach((match) =>
			set.add(decode(property, match[1]!.replace(/\s*!important\s*$/, '')))
		);
	});

	walkSheets([...doc.styleSheets], undefined, (rule) => {
		const name = rule.style[property as keyof CSSStyleDeclaration] as string;
		if (name) {
			definitions.get(rule.parentStyleSheet!)?.delete(name);
			map.set(
				name,
				new Set([...(map.get(name) ?? new Set()), ...doc.querySelectorAll(rule.selectorText)])
			);
		}
	});

	definitions.forEach((set, sheet) => {
		const styleElement = sheet.ownerNode as HTMLElement;
		if (set.size > 0) {
			const illegalNames = [...set].join(', ');
			styleElement.setAttribute(ILLEGAL_TRANSITION_NAMES, illegalNames);
			map.set('', new Set([...(map.get('') ?? new Set()), styleElement]));
		}
	});
	return map;

	function decode(prop: SupportedCSSProperties, value: string): string {
		decodeDiv.style[prop as any] = '';
		decodeDiv.style[prop as any] = value;
		const res = decodeDiv.style[prop as any];
		return res || value;
	}
}

// finds all elements of a document with a given property in their style attribute
export function elementsWithPropertyInStyleAttribute(
	doc: Document,
	property: SupportedCSSProperties,
	map: Map<string, Set<Element>> = new Map()
): Map<string, Set<Element>> {
	doc.querySelectorAll(`[style*="${property}:"`).forEach((el) => {
		const name = (el as HTMLElement).style[property as any]!;
		map.set(name, new Set([...(map.get(name) ?? new Set()), el]));
	});
	return map;
}

// finds all elements of _an active_ document with a given property
// in their style attribute or in a style sheet
export function elementsWithStyleProperty(
	doc: Document,
	property: SupportedCSSProperties,
	map: Map<string, Set<Element>> = new Map()
): Map<string, Set<Element>> {
	return elementsWithPropertyInStyleAttribute(
		doc,
		property,
		elementsWithPropertyInStylesheet(doc, property, map)
	);
}
