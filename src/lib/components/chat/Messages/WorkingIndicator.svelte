<script lang="ts">
	import { onDestroy } from 'svelte';
	import { getContext } from 'svelte';
	import { WEBUI_BASE_URL } from '$lib/constants';

	const i18n = getContext('i18n');

	// Molotych: a single hammer-and-nail animation for every working model.
	// Theme variants are the same animation; still frames respect reduced motion.
	const variant = { name: 'hammer-c', size: 64 };
	const src = (theme: 'dark' | 'light', ext: 'gif' | 'png') =>
		`${WEBUI_BASE_URL}/assets/working/${variant.name}-${theme}.${ext}`;

	// Mologate: live elapsed timer beside the hammer while the model works.
	// `startedAt` is a ms epoch timestamp; when missing we count from mount.
	export let startedAt: number | null | undefined = undefined;

	const formatElapsed = (ms: number): string => {
		const totalSeconds = Math.max(0, Math.floor(ms / 1000));
		if (totalSeconds < 60) return `${totalSeconds}s`;
		const minutes = Math.floor(totalSeconds / 60);
		const seconds = totalSeconds % 60;
		if (minutes < 60) return `${minutes}m ${String(seconds).padStart(2, '0')}s`;
		const hours = Math.floor(minutes / 60);
		return `${hours}h ${String(minutes % 60).padStart(2, '0')}m`;
	};

	const mountTime = Date.now();
	let elapsedMs = 0;

	$: effectiveStart = Number.isFinite(startedAt) ? (startedAt as number) : mountTime;
	$: elapsedMs = Math.max(0, Date.now() - effectiveStart);

	const interval = setInterval(() => {
		elapsedMs = Math.max(0, Date.now() - effectiveStart);
	}, 1000);

	onDestroy(() => clearInterval(interval));
</script>

<div
	role="status"
	aria-label={`${$i18n.t('Working')} ${formatElapsed(elapsedMs)}`}
	class="working-wrap my-1 flex items-end gap-2"
>
	<div class="working-indicator" style="--working-size: {variant.size / 16}rem">
		<img src={src('light', 'gif')} alt="" class="for-light anim" />
		<img src={src('dark', 'gif')} alt="" class="for-dark anim" />
		<img src={src('light', 'png')} alt="" class="for-light still" />
		<img src={src('dark', 'png')} alt="" class="for-dark still" />
	</div>

	<!-- Mologate: elapsed working time; monospace + tabular digits avoid jitter -->
	<span
		class="working-timer pb-2 font-mono text-xs text-gray-500 dark:text-gray-400 tabular-nums"
	>
		{formatElapsed(elapsedMs)}
	</span>
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
