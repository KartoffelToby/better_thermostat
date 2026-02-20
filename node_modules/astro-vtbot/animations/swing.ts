import './swing.css';
import {
	type NamedAnimationPairs,
	extend,
	styleSheet,
	setKeyframes,
	setStyles,
} from './animation-style';
import type { TransitionAnimation, TransitionDirectionalAnimations } from 'astro';

type SwingKeyframeParameter = {
	axis?: { x?: number; y?: number; z?: number };
	angle?: { leave?: string; enter?: string };
	opacity?: { mid?: number; to?: number };
};

type CustomSwingOptions = {
	keyframes?: SwingKeyframeParameter | string;
	base?: AnimationProperties;
	extensions?: NamedAnimationPairs;
};

export const genKeyframes = (
	keyframeNamePrefix: string,
	x: number = 0,
	y: number = 0,
	z: number = 0,
	leaveAngle = '90deg',
	enterAngle = '-90deg',
	midOpacity: number = 1,
	toOpacity: number = 0
) =>
	setKeyframes(
		keyframeNamePrefix,
		`@keyframes ${keyframeNamePrefix}FwdSwingOut {
		from {
			transform: rotate3d(${x}, ${y}, ${z}, 0);
			opacity: 1;
		}
		50% {
			opacity: ${midOpacity};
		}
		to {
			transform: rotate3d(${x}, ${y}, ${z}, ${leaveAngle});
			opacity: ${toOpacity};
		}
	}
	@keyframes ${keyframeNamePrefix}FwdSwingIn {
		from {
			transform: rotate3d(${x}, ${y}, ${z}, ${enterAngle});
			opacity: ${toOpacity};
		}
		50% {
			opacity: ${midOpacity};
		}
		to {
			transform: rotate3d(${x}, ${y}, ${z}, 0);
			opacity: 1;
		}
	}
	@keyframes ${keyframeNamePrefix}BwdSwingOut {
		from {
			transform: rotate3d(${x}, ${y}, ${z}, 0);
			opacity: 1;
		}
		50% {
			opacity: ${midOpacity};
		}
		to {
			transform: rotate3d(${x}, ${y}, ${z}, ${enterAngle});
			opacity: ${toOpacity};
		}
	}
	@keyframes ${keyframeNamePrefix}BwdSwingIn {
		from {
			transform: rotate3d(${x}, ${y}, ${z}, ${leaveAngle});
			opacity: ${toOpacity};
		}
		50% {
			opacity: ${midOpacity};
		}
		to {
			transform: rotate3d(${x}, ${y}, ${z}, 0);
			opacity: 1;
		}
	}
	::view-transition-image-pair(*) {
		perspective: 50cm;
	}
	`
	);

export type AnimationProperties = Omit<TransitionAnimation, 'name'>;

export const swing = (animation?: AnimationProperties) => namedSwing('', animation);
export const namedSwing = (keyframeNamePrefix: string, animation?: AnimationProperties) => {
	const common = {
		easing: 'ease-in-out',
		fillMode: 'both',
		duration: '0.15s',
		...animation,
	};

	const forwards = {
		old: { ...common, name: `${keyframeNamePrefix}FwdSwingOut` },
		new: { delay: common.duration, ...common, name: `${keyframeNamePrefix}FwdSwingIn` },
	};

	const backwards = {
		old: { ...common, name: `${keyframeNamePrefix}BwdSwingOut` },
		new: { delay: common.duration, ...common, name: `${keyframeNamePrefix}BwdSwingIn` },
	};
	return { forwards, backwards } as TransitionDirectionalAnimations;
};

export const customSwing = (
	transitionName: string,
	options: CustomSwingOptions,
	scope?: string
) => {
	const { keyframes, base, extensions } = options;

	let keyframeNamePrefix: string;

	if (typeof keyframes === 'string') {
		keyframeNamePrefix = keyframes;
	} else {
		keyframeNamePrefix = transitionName;
		const axis = keyframes?.axis ?? { y: 1 };
		genKeyframes(
			keyframeNamePrefix,
			axis?.x,
			axis?.y,
			axis?.z,
			keyframes?.angle?.leave,
			keyframes?.angle?.enter,
			keyframes?.opacity?.mid,
			keyframes?.opacity?.to
		);
	}

	const animations = extend(namedSwing(keyframeNamePrefix, base), extensions ?? {});
	const { scope: finalScope, styles } = styleSheet({ transitionName, scope, animations });
	setStyles(transitionName, styles);
	return finalScope;
};
