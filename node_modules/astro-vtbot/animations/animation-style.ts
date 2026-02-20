import type { TransitionAnimationPair, TransitionDirectionalAnimations } from 'astro';

type Kebab<T extends string, A extends string = ''> = T extends `${infer F}${infer R}`
	? Kebab<R, `${A}${F extends Lowercase<F> ? '' : '-'}${Lowercase<F>}`>
	: A;
type KebabKeys<T> = { [K in keyof T as K extends string ? Kebab<K> : K]: T[K] };
type AnimationCSS = KebabKeys<Partial<CSSStyleDeclaration>>;
export type NamedAnimationPairs = Record<string, { new?: AnimationCSS; old?: AnimationCSS }>;
export type ScopeAndStyles = {
	scope: string;
	styles: string;
};
export type StyleSheetOptions = {
	transitionName: string;
	animations: NamedAnimationPairs;
	scope: string | undefined;
};

export function styleSheet(options: StyleSheetOptions): ScopeAndStyles {
	const scope = options.scope ?? 'astro-' + Math.random().toString(36).slice(2, 8);
	const { transitionName, animations } = options;

	const header = `[data-astro-transition-scope=${scope}] {view-transition-name: ${transitionName};}@layer astro {`;
	const closeLayer = '}';

	const the = (dir: string) =>
		dir === 'forwards'
			? ''
			: dir === 'backwards'
				? '[data-astro-transition=back]'
				: `[data-astro-transition=${dir}`;

	const pseudo = (image: string) => `::view-transition-${image}(${transitionName})`;

	const style = (dir: string, image: string) =>
		Object.entries((animations[dir] as any)[image])
			.map(([k, v]) => `${k}: ${v}`)
			.join('; ');

	const fallback = (dir: string, image: string, scope: string) =>
		`${the(
			dir
		)}[data-astro-transition-fallback=${image}][data-astro-transition-scope=${scope}], ${the(
			dir
		)}[data-astro-transition-fallback=${image}] [data-astro-transition-scope=${scope}]`;

	let styles = header;
	const dirs = Object.keys(animations);
	styles = dirs.reduce(
		(acc, dir) => acc + `${the(dir)}${pseudo('old')} {${style(dir, 'old')}}`,
		styles
	);
	styles = dirs.reduce(
		(acc, dir) => acc + `${the(dir)}${pseudo('new')} {${style(dir, 'new')}}`,
		styles
	);
	styles += closeLayer;

	styles = dirs.reduce(
		(acc, dir) => acc + `${fallback(dir, 'old', scope)} {${style(dir, 'old')}}`,
		styles
	);
	styles = dirs.reduce(
		(acc, dir) => acc + `${fallback(dir, 'new', scope)} {${style(dir, 'new')}}`,
		styles
	);

	return { scope, styles };
}
const map: Record<string, string> = {};
map['name'] = 'animation-name';
map['delay'] = 'animation-delay';
map['duration'] = 'animation-duration';
map['easing'] = 'animation-timing-function';
map['fillMode'] = 'animation-fill-mode';
map['direction'] = 'animation-direction';
const timeString = (value: number | string) => (typeof value === 'number' ? value + 'ms' : value);

export const extend = (base: TransitionDirectionalAnimations, extension?: NamedAnimationPairs) => {
	if (
		Array.isArray(base.forwards.new) ||
		Array.isArray(base.forwards.old) ||
		Array.isArray(base.backwards.new) ||
		Array.isArray(base.backwards.old)
	) {
		throw new Error('extend() can only handle animation objects, not arrays');
	}
	return {
		forwards: {
			old: Object.fromEntries([
				...Object.entries(base.forwards.old).map(([key, value]) => [map[key], timeString(value)]),
				...Object.entries(extension?.forwards?.old ?? {}),
			]),
			new: Object.fromEntries([
				...Object.entries(base.forwards.new).map(([key, value]) => [map[key], timeString(value)]),
				...Object.entries(extension?.forwards?.new ?? {}),
			]),
		},
		backwards: {
			old: Object.fromEntries([
				...Object.entries(base.backwards.old).map(([key, value]) => [map[key], value]),
				...Object.entries(extension?.backwards?.old ?? {}),
			]),
			new: Object.fromEntries([
				...Object.entries(base.backwards.new).map(([key, value]) => [map[key], value]),
				...Object.entries(extension?.backwards?.new ?? {}),
			]),
		},
	};
};

export const adapter = (
	anims: Record<string, TransitionAnimationPair>
): TransitionDirectionalAnimations => anims as unknown as TransitionDirectionalAnimations;

const framesMap: Record<string, string> = {};
export const setKeyframes = (name: string, css: string) => (framesMap[name] = css);
export const getKeyframes = (name: string) => framesMap[name];

const stylesMap: Record<string, string> = {};
export const setStyles = (name: string, css: string) => (stylesMap[name] = css);
export const getStyles = (name: string) => stylesMap[name];
