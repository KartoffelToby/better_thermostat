import './move.css';
import {
	type NamedAnimationPairs,
	extend,
	styleSheet,
	setKeyframes,
	setStyles,
} from './animation-style';
import type { TransitionAnimation, TransitionDirectionalAnimations } from 'astro';

type MoveKeyframeParameter = {
	enter?: { x?: string; y?: string; z?: string; angel?: string };
	enterMid?: { x?: string; y?: string; z?: string; angel?: string };
	leaveMid?: { x?: string; y?: string; z?: string; angel?: string };
	leave?: { x?: string; y?: string; z?: string; angel?: string };
	axis?: { x?: number; y?: number; z?: number };
};

type CustomMoveOptions = {
	keyframes?: MoveKeyframeParameter | string;
	base?: AnimationProperties;
	extensions?: NamedAnimationPairs;
};

export const genKeyframes = (
	keyframeNamePrefix: string,
	enterX = '0',
	enterY = '0cm',
	enterZ = '0cm',
	enterXm = '0',
	enterYm = '0cm',
	enterZm = '0cm',
	leaveXm = '0',
	leaveYm = '0cm',
	leaveZm = '0cm',
	leaveX = '0',
	leaveY = '0cm',
	leaveZ = '0cm',
	x = 0,
	y = 0,
	z = 1,
	enterA = '0deg',
	enterAm = '0deg',
	leaveAm = '0deg',
	leaveA = '0deg'
) =>
	setKeyframes(
		keyframeNamePrefix,
		`
		@keyframes ${keyframeNamePrefix}FwdMoveOut {
		from {
			transform: translate3d(0, 0, 0)	rotate3d(1, 0, 0, 0);
			opacity: 1;
		}
		50% {
			transform: translate3d(${leaveXm}, ${leaveYm}, ${leaveZm}) rotate3d(${x}, ${y}, ${z}, ${leaveAm});
			opacity: 1;
		}
		to {
			transform: translate3d(${leaveX}, ${leaveY}, ${leaveZ}) rotate3d(${x}, ${y}, ${z}, ${leaveA});
			opacity: 0;
		}
	}
	@keyframes ${keyframeNamePrefix}FwdMoveIn {
		from {
			transform: translate3d(${enterX}, ${enterY}, ${enterZ}) rotate3d(${x}, ${y}, ${z}, ${enterA});
			opacity: 0;
		}
		50% {
			transform: translate3d(${enterXm}, ${enterYm}, ${enterZm}) rotate3d(${x}, ${y}, ${z}, ${enterAm});
			opacity: 1;
		}
		to {
			transform: translate3d(0, 0, 0) rotate3d(1,0,0,0);
			opacity: 1;
		}
	}
	@keyframes ${keyframeNamePrefix}BwdMoveOut {
		from {
			transform: translate3d(0, 0, 0) rotate3d(1,0,0,0);
			opacity: 1;
		}
		50% {
			transform: translate3d(${enterXm}, ${enterYm}, ${enterZm}) rotate3d(${x}, ${y}, ${z}, ${enterAm});
			opacity: 1;
		}
		to {
			transform: translate3d(${enterX}, ${enterY}, ${enterZ}) rotate3d(${x}, ${y}, ${z}, ${enterA});
			opacity: 0;
		}
	}
	@keyframes ${keyframeNamePrefix}BwdMoveIn {
		from {
			transform: translate3d(${leaveX}, ${leaveY}, ${leaveZ}) rotate3d(${x}, ${y}, ${z}, ${leaveA});
			opacity: 0;
		}
		50% {
			transform: translate3d(${leaveXm}, ${leaveYm}, ${leaveZm}) rotate3d(${x}, ${y}, ${z}, ${leaveAm});
			opacity: 1;
		}
		to {
			transform: translate3d(0, 0, 0) rotate3d(1, 0, 0, 0);
			opacity: 1;
		}
	}
	::view-transition-image-pair(*) {
		perspective: 50cm;
	}
	`
	);

export type AnimationProperties = Omit<TransitionAnimation, 'name'>;

export const move = (animation?: AnimationProperties) => namedMove('', animation);
export const namedMove = (keyframeNamePrefix: string, animation?: AnimationProperties) => {
	const common = {
		easing: 'ease-in-out',
		fillMode: 'both',
		duration: '0.3s',
		...animation,
	};

	const forwards = {
		old: { ...common, name: `${keyframeNamePrefix}FwdMoveOut` },
		new: { ...common, name: `${keyframeNamePrefix}FwdMoveIn` },
	};

	const backwards = {
		old: { ...common, name: `${keyframeNamePrefix}BwdMoveOut` },
		new: { ...common, name: `${keyframeNamePrefix}BwdMoveIn` },
	};
	return { forwards, backwards } as TransitionDirectionalAnimations;
};

export const customMove = (transitionName: string, options?: CustomMoveOptions, scope?: string) => {
	const { keyframes, base, extensions } = options ?? {};

	let keyframeNamePrefix: string;

	if (typeof keyframes === 'string') {
		keyframeNamePrefix = keyframes;
	} else {
		keyframeNamePrefix = transitionName;
		genKeyframes(
			keyframeNamePrefix,
			keyframes?.enter?.x,
			keyframes?.enter?.y,
			keyframes?.enter?.z,
			keyframes?.enterMid?.x,
			keyframes?.enterMid?.y,
			keyframes?.enterMid?.z,
			keyframes?.leaveMid?.x,
			keyframes?.leaveMid?.y,
			keyframes?.leaveMid?.z,
			keyframes?.leave?.x,
			keyframes?.leave?.y,
			keyframes?.leave?.z,
			keyframes?.axis?.x,
			keyframes?.axis?.y,
			keyframes?.axis?.z,
			keyframes?.enter?.angel,
			keyframes?.enterMid?.angel,
			keyframes?.leaveMid?.angel,
			keyframes?.leave?.angel
		);
	}

	const animations = extend(namedMove(keyframeNamePrefix, base), extensions ?? {});
	const { scope: finalScope, styles } = styleSheet({ transitionName, scope, animations });
	setStyles(transitionName, styles);
	return finalScope;
};
