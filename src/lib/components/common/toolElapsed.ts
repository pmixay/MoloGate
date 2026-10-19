export type Seconds = number | string | null | undefined;

export function secondsToMs(value: Seconds): number | null {
	if (value === null || value === undefined || value === '') return null;
	const number = Number(value);
	return Number.isFinite(number) && number >= 0 ? number * 1000 : null;
}

export function toolElapsedMs(
	start: Seconds,
	end: Seconds,
	duration: Seconds,
	done: boolean,
	now: number
): number | null {
	const started = secondsToMs(start);
	const ended = secondsToMs(end);
	if (started !== null && ended !== null) return Math.max(0, ended - started);
	const elapsed = secondsToMs(duration);
	if (done) return elapsed;
	return started === null ? null : Math.max(0, now - started);
}
