import './zoom.css';
import {
	type NamedAnimationPairs,
	extend,
	styleSheet,
	setKeyframes,
	setStyles,
} from './animation-style';
import type { TransitionAnimation, TransitionDirectionalAnimations } from 'astro';

type ZoomKeyframeParameter = {
	scale?: { forwardOut?: number; forwardIn?: number; backwardOut?: number; backwardIn?: number };
	opacity?: { forwardOut?: number; forwardIn?: number; backwardOut?: number; backwardIn?: number };
};

type CustomZoomOptions = {
	keyframes?: ZoomKeyframeParameter | string;
	base?: AnimationProperties;
	extensions?: NamedAnimationPairs;
};

const genKeyframes = (
	keyframeNamePrefix: string,
	forwardOutScale = 5,
	forwardOutOpacity = 0,
	forwardInScale = 0,
	forwardInOpacity = 0,
	backwardOutScale = 0,
	backwardOutOpacity = 0,
	backwardInScale = 5,
	backwardInOpacity = 0
) =>
	setKeyframes(
		keyframeNamePrefix,
		`
	@keyframes ${keyframeNamePrefix}FwdZoomOut {
		from {
			transform: scale(1);
			opacity: 1;
		}
		to {
			transform: scale(${forwardOutScale});
			opacity: ${forwardOutOpacity};
		}
	}
	@keyframes ${keyframeNamePrefix}FwdZoomIn {
		from {
			transform: scale(${forwardInScale});
			opacity: ${forwardInOpacity};
		}
		to {
			transform: scale(1);
			opacity: 1;
		}
	}
	@keyframes ${keyframeNamePrefix}BwdZoomOut {
		from {
			transform: scale(1);
			opacity: 1;
		}
		to {
			transform: scale(${backwardOutScale});
			opacity: ${backwardOutOpacity};
		}
	}
	@keyframes ${keyframeNamePrefix}BwdZoomIn {
		from {
			transform: scale(${backwardInScale});
			opacity: ${backwardInOpacity};
		}
		to {
			transform: scale(1);
			opacity: 1;
		}
	}`
	);

type AnimationProperties = Omit<TransitionAnimation, 'name'>;

export const zoom = (animation?: AnimationProperties) => namedZoom('', animation);
const namedZoom = (keyframeNamePrefix: string, animation?: AnimationProperties) => {
	const common = {
		easing: 'ease-in-out',
		fillMode: 'both',
		duration: '0.3s',
		...animation,
	};

	const forwards = {
		old: { ...common, name: `${keyframeNamePrefix}FwdZoomOut` },
		new: { ...common, name: `${keyframeNamePrefix}FwdZoomIn` },
	};

	const backwards = {
		old: { ...common, name: `${keyframeNamePrefix}BwdZoomOut` },
		new: { ...common, name: `${keyframeNamePrefix}BwdZoomIn` },
	};
	return { forwards, backwards } as TransitionDirectionalAnimations;
};

export const customZoom = (transitionName: string, options: CustomZoomOptions, scope?: string) => {
	const { keyframes, base, extensions } = options;

	let keyframeNamePrefix: string;

	if (typeof keyframes === 'string') {
		keyframeNamePrefix = keyframes;
	} else {
		keyframeNamePrefix = transitionName;
		const { scale, opacity } = keyframes ?? {};
		genKeyframes(
			transitionName,
			scale?.forwardOut,
			opacity?.forwardOut,
			scale?.forwardIn,
			opacity?.forwardIn,
			scale?.backwardOut,
			opacity?.backwardOut,
			scale?.backwardIn,
			opacity?.backwardIn
		);
	}

	const animations = extend(namedZoom(keyframeNamePrefix, base), extensions ?? {});
	const { scope: finalScope, styles } = styleSheet({ transitionName, scope, animations });
	setStyles(transitionName, styles);
	return finalScope;
};
