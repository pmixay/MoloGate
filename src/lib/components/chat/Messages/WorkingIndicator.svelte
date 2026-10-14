<script lang="ts">
	import { getContext } from 'svelte';
	import { WEBUI_BASE_URL } from '$lib/constants';

	const i18n = getContext('i18n');

	// Mologate: one of three pixel-art hammer animations, picked at random for each reply. Each is
	// shown at its own size in pixels, so one art pixel is one CSS pixel in all of them.
	const variants = [
		{ name: 'hammer-a', size: 48 },
		{ name: 'hammer-b', size: 48 },
		// The hammer jumps on a nail until it's driven in, then a new nail comes.
		{ name: 'hammer-c', size: 64 }
	];
	const variant = variants[Math.floor(Math.random() * variants.length)];
	const src = (theme: 'dark' | 'light', ext: 'gif' | 'png') =>
		`${WEBUI_BASE_URL}/assets/working/${variant.name}-${theme}.${ext}`;
</script>

<div
	role="status"
	aria-label={$i18n.t('Working')}
	class="working-indicator my-1"
	style="--working-size: {variant.size / 16}rem"
>
	<img src={src('light', 'gif')} alt="" class="for-light anim" />
	<img src={src('dark', 'gif')} alt="" class="for-dark anim" />
	<img src={src('light', 'png')} alt="" class="for-light still" />
	<img src={src('dark', 'png')} alt="" class="for-dark still" />
</div>

<style>
	/* Show the animation for the current theme; a still frame when motion is reduced. */
	.working-indicator {
		width: var(--working-size);
		height: var(--working-size);
	}

	.working-indicator img {
		display: none;
		width: var(--working-size);
		height: var(--working-size);
		image-rendering: pixelated;
	}

	.working-indicator .for-light.anim {
		display: block;
	}

	:global(.dark) .working-indicator .for-light.anim {
		display: none;
	}

	:global(.dark) .working-indicator .for-dark.anim {
		display: block;
	}

	@media (prefers-reduced-motion: reduce) {
		.working-indicator img.anim {
			display: none !important;
		}

		.working-indicator .for-light.still {
			display: block;
		}

		:global(.dark) .working-indicator .for-light.still {
			display: none;
		}

		:global(.dark) .working-indicator .for-dark.still {
			display: block;
		}
	}
</style>
