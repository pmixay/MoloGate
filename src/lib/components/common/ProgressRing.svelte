<script lang="ts">
	// Mologate: determinate progress, 0 to 1, as a small ring (upload progress on file chips).
	export let progress = 0;
	export let size = 16;

	const radius = 8;
	const circumference = 2 * Math.PI * radius;
	$: clamped = Math.min(1, Math.max(0, progress || 0));
</script>

<svg
	width={size}
	height={size}
	viewBox="0 0 20 20"
	class="-rotate-90 shrink-0"
	role="progressbar"
	aria-valuemin="0"
	aria-valuemax="100"
	aria-valuenow={Math.round(clamped * 100)}
>
	<circle
		cx="10"
		cy="10"
		r={radius}
		fill="none"
		stroke-width="2.5"
		class="stroke-current opacity-20"
	/>
	<circle
		cx="10"
		cy="10"
		r={radius}
		fill="none"
		stroke-width="2.5"
		stroke-linecap="round"
		class="stroke-current transition-[stroke-dasharray] duration-200"
		stroke-dasharray={`${Math.max(clamped, 0.02) * circumference} ${circumference}`}
	/>
</svg>
