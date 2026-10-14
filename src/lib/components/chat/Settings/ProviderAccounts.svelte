<script lang="ts">
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { getModels } from '$lib/apis';
	import { models, settings, config, user } from '$lib/stores';
	import {
		ProviderAccountError,
		providerAccountRequest,
		type AutoReset,
		type ProviderAccount,
		type DeviceLogin,
		type MyUsage,
		type Sharing
	} from '$lib/apis/provider-accounts';
	import Switch from '$lib/components/common/Switch.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import ArrowPath from '$lib/components/icons/ArrowPath.svelte';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	// Mologate: the same wording as the chat's limits panel ("Weekly limit", "Monthly budget").
	import {
		amountText,
		formatCount,
		formatDollars,
		formatPercent,
		limitLabel,
		mainWindows,
		shortLabel,
		type LimitWindow
	} from '$lib/utils/providerLimits';

	const providers: Record<string, string> = {
		codex: 'ChatGPT',
		anthropic_api: 'Anthropic',
		openai_api: 'OpenAI',
		openrouter: 'OpenRouter',
		zai: 'Z.ai'
	};
	const plans: Record<string, string> = {
		api: 'API key',
		lite: 'Coding Lite',
		pro: 'Pro',
		max: 'Max',
		plus: 'Plus',
		prolite: 'Pro Lite',
		team: 'Team',
		business: 'Business',
		enterprise: 'Enterprise',
		edu: 'Edu',
		free: 'Free'
	};
	const buttonClass =
		'rounded-full border border-gray-200 px-3 py-1 font-medium text-gray-700 transition hover:bg-gray-50 disabled:opacity-50 dark:border-gray-700 dark:text-gray-200 dark:hover:bg-gray-800';
	const dangerButtonClass =
		'rounded-full border border-red-200 px-3 py-1 font-medium text-red-600 transition hover:bg-red-50 disabled:opacity-50 dark:border-red-900/60 dark:text-red-400 dark:hover:bg-red-950/40';
	const sharingOptions: { value: Sharing; label: string }[] = [
		{ value: 'private', label: 'Only me' },
		{ value: 'shared', label: 'Team' },
		{ value: 'paused', label: 'Paused' }
	];

	let accounts: ProviderAccount[] = [];
	let autoReset: AutoReset | null = null;
	let myUsage: MyUsage | null = null;
	let loading = true;
	let busy = false;
	let error = '';
	let connectOpen = false;
	let provider = 'codex';
	let apiKey = '';
	let budget = 20;
	let plan = 'api';
	let shareWithTeam = false;
	// Mologate: an admin's account that belongs to no member and serves the whole team.
	let teamAccount = false;
	let login: DeviceLogin | null = null;
	let loginMethod: 'link' | 'device_code' = 'link';
	let callbackUrl = '';
	let expanded: Record<string, boolean> = {};
	let disconnectId = '';
	let editingId = '';
	let editedBudget = 20;
	let active = true;
	let polling = false;

	const request = <T,>(path = '', method = 'GET', body?: object) =>
		providerAccountRequest<T>(localStorage.token, path, method, body);
	const message = (err: unknown) =>
		err instanceof Error ? err.message : 'The account request failed.';
	// Mologate: a sign-in the server no longer has (expired or restarted) must not block a new one.
	const loginGone = (err: unknown) => err instanceof ProviderAccountError && err.status === 404;
	const forgetLogin = () => {
		localStorage.removeItem(`mologate-device-login:${$user?.id}`);
		login = null;
		callbackUrl = '';
	};

	const planName = (value: string) =>
		plans[value] ?? value.charAt(0).toUpperCase() + value.slice(1);
	const ownerName = (account: ProviderAccount) =>
		account.owner_user_id === $user?.id ? 'You' : account.owner_name;
	// A team account has no owner: no reserve, every use is the team's.
	const teamOptions = sharingOptions.filter((option) => option.value !== 'private');
	const resetText = (resetsAt?: number) => {
		if (!resetsAt) return '';
		const seconds = resetsAt - Date.now() / 1000;
		if (seconds <= 0) return 'resetting now';
		const minutes = Math.ceil(seconds / 60);
		if (minutes < 60) return `resets in ${minutes} min`;
		const hours = Math.floor(minutes / 60);
		if (hours < 24) return `resets in ${hours} h${minutes % 60 ? ` ${minutes % 60} min` : ''}`;
		return `resets ${new Date(resetsAt * 1000).toLocaleString(undefined, {
			weekday: 'short',
			hour: '2-digit',
			minute: '2-digit'
		})}`;
	};
	const barColor = (used: number) =>
		used >= 90 ? 'bg-red-500' : used >= 70 ? 'bg-amber-500' : 'bg-emerald-500';
	// Mologate: half of a shared account's 5-hour and weekly windows is the owner's reserve and half
	// is the team pool (docs/limits-and-accounts.md, decision 2). Owner use past their half is pool use.
	const RESERVE = 50;
	const splitOf = (account: ProviderAccount, window: LimitWindow) => {
		if (account.sharing !== 'shared' || !account.owner_user_id || window.team_percent == null)
			return null;
		const owner = Math.max(0, window.used_percent - window.team_percent);
		return {
			reserve: Math.min(owner, RESERVE),
			pool: window.team_percent + Math.max(0, owner - RESERVE)
		};
	};
	// How full each bar is: the reserve and pool halves of a shared window, or the whole window.
	const barFills = (account: ProviderAccount, window: LimitWindow) => {
		const split = splitOf(account, window);
		return split
			? [(split.reserve / RESERVE) * 100, (split.pool / (100 - RESERVE)) * 100]
			: [window.used_percent];
	};
	const splitTitle = (account: ProviderAccount, window: LimitWindow) => {
		const split = splitOf(account, window);
		return split
			? `${limitLabel(window)}: ${ownerName(account)} ${Math.round(split.reserve)} of ${RESERVE}, team ${Math.round(split.pool)} of ${100 - RESERVE}`
			: `${limitLabel(window)}: ${Math.round(window.used_percent)}% used`;
	};
	const statusOf = (account: ProviderAccount) =>
		account.sharing === 'paused'
			? { text: 'Paused', dot: 'bg-gray-400' }
			: account.status === 'ok'
				? { text: 'Connected', dot: 'bg-emerald-500' }
				: account.status === 'needs_login'
					? { text: 'Sign-in required', dot: 'bg-amber-500' }
					: { text: account.status, dot: 'bg-red-500' };
	// Mologate: only what is worth reading at a glance; a working, private account needs no note.
	const rowNote = (account: ProviderAccount) =>
		account.sharing === 'paused'
			? 'Paused'
			: account.status !== 'ok'
				? statusOf(account).text
				: account.sharing === 'shared' && account.owner_user_id
					? 'Team'
					: '';
	const couponTitle = (account: ProviderAccount) => {
		const count = account.limits?.reset_credits ?? 0;
		const last = account.last_auto_reset_at
			? ` Last used ${new Date(account.last_auto_reset_at * 1000).toLocaleDateString()}.`
			: '';
		return `${count} reset ${count === 1 ? 'coupon' : 'coupons'} for auto-reset.${last}`;
	};

	const refresh = async (refreshModels = false) => {
		[accounts, autoReset, myUsage] = await Promise.all([
			request<{ data: ProviderAccount[] }>().then((result) => result.data),
			request<AutoReset>('/auto-reset'),
			// Mologate: numbers only; the page works without them.
			request<MyUsage>('/usage').catch(() => null)
		]);
		if (refreshModels) {
			models.set(
				await getModels(
					localStorage.token,
					$config?.features?.enable_direct_connections ? $settings?.directConnections : null
				)
			);
		}
	};
	const poll = async () => {
		if (!active || polling || !login || login.status !== 'pending') return;
		polling = true;
		const id = login.id;
		try {
			const next = await request<DeviceLogin>(`/codex/logins/${id}`);
			// Ignore a reply for a sign-in that was cancelled or replaced meanwhile.
			if (!active || login?.id !== id) return;
			await applyLogin(next);
		} catch (err) {
			if (!active || login?.id !== id) return;
			if (loginGone(err)) forgetLogin();
			error = loginGone(err) ? 'Sign-in expired. Start a new connection.' : message(err);
		} finally {
			polling = false;
		}
	};
	const applyLogin = async (next: DeviceLogin) => {
		login = next;
		if (next.status === 'pending') return;
		forgetLogin();
		if (next.status === 'completed') {
			connectOpen = false;
			await refresh(true);
			toast.success('ChatGPT account connected. Its models are ready to use.');
		} else {
			error = next.error || 'Sign-in expired. Start a new connection.';
		}
	};
	// Mologate: the browser's failed localhost page carries the sign-in result back to Mologate.
	const finishLink = async () => {
		if (!login) return;
		busy = true;
		error = '';
		try {
			await applyLogin(
				await request<DeviceLogin>(`/codex/logins/${login.id}/callback`, 'POST', {
					callback_url: callbackUrl
				})
			);
		} catch (err) {
			if (loginGone(err)) forgetLogin();
			error = loginGone(err) ? 'Sign-in expired. Start a new connection.' : message(err);
		} finally {
			busy = false;
		}
	};
	const cancelLogin = async () => {
		if (!login) return;
		busy = true;
		error = '';
		try {
			await request(`/codex/logins/${login.id}`, 'DELETE');
			forgetLogin();
		} catch (err) {
			if (loginGone(err)) forgetLogin();
			else error = message(err);
		} finally {
			busy = false;
		}
	};
	// Mologate: accounts are named by the server ("Account #1n9Dk5"); members choose no labels.
	const connect = async () => {
		busy = true;
		error = '';
		const sharing: Sharing = shareWithTeam ? 'shared' : 'private';
		try {
			if (provider === 'codex') {
				login = await request<DeviceLogin>('/codex', 'POST', {
					sharing,
					method: loginMethod,
					team: teamAccount
				});
				localStorage.setItem(`mologate-device-login:${$user?.id}`, login.id);
			} else {
				await request('', 'POST', {
					provider,
					api_key: apiKey,
					plan: provider === 'zai' ? plan : 'api',
					sharing,
					team: teamAccount,
					budget: provider === 'zai' && plan !== 'api' ? null : { amount: budget, currency: 'USD' }
				});
				apiKey = '';
				connectOpen = false;
				await refresh(true);
				toast.success('Provider account connected.');
			}
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	};
	const change = async (account: ProviderAccount, body: object) => {
		busy = true;
		error = '';
		try {
			await request(`/${account.id}`, 'PATCH', body);
			editingId = '';
			await refresh(true);
		} catch (err) {
			error = message(err);
			await refresh().catch(() => {});
		} finally {
			busy = false;
		}
	};
	// Mologate: any approved member can switch automatic resets for the whole team.
	const setAutoReset = async (enabled: boolean) => {
		busy = true;
		error = '';
		try {
			autoReset = await request<AutoReset>('/auto-reset', 'PUT', { enabled });
			toast.success(enabled ? 'Auto-reset limits is on.' : 'Auto-reset limits is off.');
		} catch (err) {
			error = message(err);
			await refresh().catch(() => {});
		} finally {
			busy = false;
		}
	};
	const verify = async (account: ProviderAccount) => {
		busy = true;
		error = '';
		try {
			await request(`/${account.id}/verify`, 'POST');
			await refresh(true);
			toast.success('Account verified.');
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	};
	const disconnect = async (account: ProviderAccount) => {
		busy = true;
		error = '';
		try {
			await request(`/${account.id}`, 'DELETE');
			disconnectId = '';
			await refresh(true);
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	};
	onMount(() => {
		active = true;
		refresh()
			.catch((err) => {
				error = message(err);
			})
			.finally(() => {
				loading = false;
			});
		const pendingId = localStorage.getItem(`mologate-device-login:${$user?.id}`);
		if (pendingId) {
			login = { id: pendingId, status: 'pending', expires_at: 0 };
			poll();
		}
		const timer = setInterval(poll, 3000);
		return () => {
			active = false;
			clearInterval(timer);
			apiKey = '';
		};
	});
</script>

<!-- Mologate: the settings panel clips its content; this tab scrolls inside its own area. -->
<div class="flex h-full min-h-0 flex-col text-sm" id="tab-provider_accounts">
	<div class="mb-4 flex items-center justify-between gap-3">
		<h2 class="text-sm font-medium text-gray-900 dark:text-white">Provider accounts</h2>
		<div class="flex shrink-0 items-center gap-3">
			{#if autoReset}
				<label
					class="flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-400"
					title={`Redeem a ChatGPT reset coupon when every account is out of quota. Applies to the whole team.${
						autoReset.changed_by ? ` Last changed by ${autoReset.changed_by}.` : ''
					}`}
				>
					Auto-reset
					<Switch
						state={autoReset.enabled}
						ariaLabel="Auto-reset limits"
						on:change={(event) => setAutoReset(event.detail)}
					/>
				</label>
			{/if}
			<button
				type="button"
				class="flex shrink-0 items-center gap-1 rounded-full bg-gray-900 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-gray-800 disabled:opacity-50 dark:bg-white dark:text-gray-900 dark:hover:bg-gray-100"
				disabled={login?.status === 'pending'}
				aria-expanded={connectOpen}
				on:click={() => {
					connectOpen = !connectOpen;
					error = '';
				}}
			>
				<Plus className="size-3" strokeWidth="2.5" />
				Connect
			</button>
		</div>
	</div>

	<div class="scrollbar-hover min-h-0 flex-1 overflow-y-auto pr-1.5">
		{#if error}
			<p
				role="alert"
				class="mb-3 rounded-xl bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-950/40 dark:text-red-300"
			>
				{error}
			</p>
		{/if}

		{#if connectOpen && login?.status !== 'pending'}
			<form
				on:submit|preventDefault={connect}
				class="mb-4 space-y-3 rounded-2xl border border-gray-100 p-3 dark:border-gray-850"
			>
				<div class="flex flex-wrap gap-1" role="radiogroup" aria-label="Provider">
					{#each Object.entries(providers) as [id, name]}
						<button
							type="button"
							role="radio"
							aria-checked={provider === id}
							class="rounded-full px-2.5 py-1 text-xs transition {provider === id
								? 'bg-gray-900 text-white dark:bg-white dark:text-gray-900'
								: 'bg-gray-50 text-gray-600 hover:bg-gray-100 dark:bg-gray-850 dark:text-gray-300 dark:hover:bg-gray-800'}"
							on:click={() => (provider = id)}>{name}</button
						>
					{/each}
				</div>

				{#if provider === 'codex'}
					<div class="flex flex-wrap gap-1" role="radiogroup" aria-label="Sign-in method">
						{#each [{ value: 'link', label: 'Sign-in link' }, { value: 'device_code', label: 'Sign-in code' }] as option}
							<button
								type="button"
								role="radio"
								aria-checked={loginMethod === option.value}
								class="rounded-full px-2.5 py-1 text-xs transition {loginMethod === option.value
									? 'bg-gray-200 text-gray-900 dark:bg-gray-700 dark:text-white'
									: 'text-gray-500 hover:bg-gray-50 dark:hover:bg-gray-850'}"
								on:click={() => (loginMethod = option.value === 'link' ? 'link' : 'device_code')}
								>{option.label}</button
							>
						{/each}
					</div>
					<p class="text-xs text-gray-500">
						{loginMethod === 'link'
							? 'Sign in on OpenAI, then paste the address your browser opens back here.'
							: "Enter a code on OpenAI's device page. Some accounts must allow device sign-in first."}
					</p>
				{:else}
					<input
						bind:value={apiKey}
						type="password"
						required
						autocomplete="off"
						spellcheck="false"
						aria-label="API key"
						placeholder="API key"
						class="w-full rounded-xl bg-gray-50 px-3 py-2 text-xs outline-hidden dark:bg-gray-850"
					/>
					<div class="flex flex-wrap items-center gap-2 text-xs text-gray-600 dark:text-gray-400">
						{#if provider === 'zai'}
							<select
								bind:value={plan}
								aria-label="Plan"
								class="rounded-xl bg-gray-50 px-2 py-1.5 outline-hidden dark:bg-gray-850"
							>
								<option value="api">API key</option><option value="lite">Coding Lite</option><option
									value="pro">Coding Pro</option
								><option value="max">Coding Max</option>
							</select>
						{/if}
						{#if provider !== 'zai' || plan === 'api'}
							<label class="flex items-center gap-1.5"
								>Monthly budget $
								<input
									bind:value={budget}
									type="number"
									min="0.01"
									max="1000000"
									step="0.01"
									required
									class="w-20 rounded-xl bg-gray-50 px-2 py-1.5 outline-hidden dark:bg-gray-850"
								/>
							</label>
						{/if}
					</div>
				{/if}

				<div class="flex items-center justify-between gap-3">
					<div class="flex flex-wrap items-center gap-x-4 gap-y-2">
						{#if $user?.role === 'admin'}
							<label
								class="flex items-center gap-2 text-xs text-gray-600 dark:text-gray-400"
								title="Belongs to no member: everyone uses it, admins manage it"
							>
								<Switch bind:state={teamAccount} ariaLabel="Team account" />
								Team account
							</label>
						{/if}
						{#if !teamAccount}
							<label class="flex items-center gap-2 text-xs text-gray-600 dark:text-gray-400">
								<Switch bind:state={shareWithTeam} ariaLabel="Share with the team" />
								Share with the team
							</label>
						{/if}
					</div>
					<div class="flex items-center gap-2">
						<button
							type="button"
							class="px-2 py-1.5 text-xs text-gray-500 hover:text-gray-900 dark:hover:text-white"
							on:click={() => (connectOpen = false)}>Cancel</button
						>
						<button
							type="submit"
							disabled={busy}
							class="flex items-center gap-1.5 rounded-full bg-gray-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50 dark:bg-white dark:text-gray-900"
						>
							{#if busy}<Spinner className="size-3" />{/if}
							{provider === 'codex' ? 'Continue' : 'Verify and connect'}
						</button>
					</div>
				</div>
			</form>
		{/if}

		{#if login?.status === 'pending'}
			<div
				class="mb-4 space-y-2 rounded-2xl border border-blue-200 p-3 text-xs dark:border-blue-900"
				role="status"
			>
				<div class="flex items-center justify-between gap-2">
					<h3 class="font-medium text-gray-900 dark:text-white">Finish the ChatGPT sign-in</h3>
					<button
						disabled={busy}
						on:click={cancelLogin}
						class="text-gray-500 hover:text-gray-900 dark:hover:text-white">Cancel</button
					>
				</div>
				{#if login.method === 'link' && login.auth_url}
					<ol class="ml-4 list-decimal space-y-1 text-gray-600 dark:text-gray-400">
						<li>
							<a
								class="text-blue-600 underline dark:text-blue-400"
								href={login.auth_url}
								target="_blank"
								rel="noopener noreferrer">Open the OpenAI sign-in link</a
							> and sign in.
						</li>
						<li>
							Your browser then opens <span class="font-mono">localhost:1455</span>, which does not
							load. That is expected.
						</li>
						<li>Copy the full address from the address bar and paste it here.</li>
					</ol>
					<form on:submit|preventDefault={finishLink} class="flex gap-2">
						<input
							bind:value={callbackUrl}
							aria-label="Address after signing in"
							placeholder="http://localhost:1455/auth/callback?code=…"
							required
							autocomplete="off"
							spellcheck="false"
							class="min-w-0 flex-1 rounded-xl bg-gray-50 px-3 py-2 font-mono outline-hidden dark:bg-gray-850"
						/>
						<button
							type="submit"
							disabled={busy || !callbackUrl.trim()}
							class="shrink-0 rounded-full bg-gray-900 px-3 py-1.5 font-medium text-white disabled:opacity-50 dark:bg-white dark:text-gray-900"
							>{busy ? 'Finishing…' : 'Finish'}</button
						>
					</form>
				{:else if login.user_code && login.verification_url}
					<p class="text-gray-600 dark:text-gray-400">
						<a
							class="text-blue-600 underline dark:text-blue-400"
							href={login.verification_url}
							target="_blank"
							rel="noopener noreferrer">Open OpenAI's device page</a
						> and enter this code:
					</p>
					<p class="select-all font-mono text-lg tracking-widest text-gray-900 dark:text-white">
						{login.user_code}
					</p>
				{:else}
					<p class="flex items-center gap-2 text-gray-500">
						<Spinner className="size-3" /> Preparing…
					</p>
				{/if}
				{#if login.expires_at}
					<p class="text-gray-400">
						Expires at {new Date(login.expires_at * 1000).toLocaleTimeString([], {
							hour: '2-digit',
							minute: '2-digit'
						})}.
					</p>
				{/if}
			</div>
		{/if}

		<section>
			<h3 class="mb-2 text-xs text-gray-600 dark:text-gray-400">
				Accounts{accounts.length ? ` · ${accounts.length}` : ''}
			</h3>
			{#if loading}
				<div class="flex justify-center py-6"><Spinner className="size-4" /></div>
			{:else if accounts.length === 0}
				<p
					class="rounded-2xl bg-gray-50 px-3 py-4 text-center text-xs text-gray-500 dark:bg-gray-850"
				>
					No accounts yet. Connect one to use its models.
				</p>
			{:else}
				<ul
					class="divide-y divide-gray-100 rounded-2xl border border-gray-100 dark:divide-gray-850 dark:border-gray-850"
				>
					{#each accounts as account (account.id)}
						{@const status = statusOf(account)}
						{@const windows = account.limits?.windows ?? []}
						<li>
							<button
								type="button"
								class="flex w-full items-center gap-3 px-3 py-2 text-left transition hover:bg-gray-50 dark:hover:bg-gray-850/60 {expanded[
									account.id
								]
									? 'bg-gray-50/70 dark:bg-gray-850/40'
									: ''}"
								aria-expanded={!!expanded[account.id]}
								on:click={() => (expanded = { ...expanded, [account.id]: !expanded[account.id] })}
							>
								<span class="size-1.5 shrink-0 rounded-full {status.dot}" title={status.text}
								></span>
								<div class="min-w-0 flex-1">
									<div class="truncate text-xs font-medium text-gray-900 dark:text-white">
										{account.label}
									</div>
									<div class="truncate text-[0.6875rem] text-gray-500">
										{providers[account.provider] ?? account.provider} · {planName(account.plan)} · {ownerName(
											account
										)}{rowNote(account) ? ` · ${rowNote(account)}` : ''}
									</div>
								</div>
								<div class="hidden shrink-0 items-center gap-3 sm:flex">
									{#each windows.slice(0, 2) as window}
										<div class="w-16" title={splitTitle(account, window)}>
											<div class="flex justify-between text-[0.625rem] text-gray-500">
												<span>{shortLabel(window)}</span><span
													>{Math.round(window.used_percent)}%</span
												>
											</div>
											<div class="mt-0.5 flex gap-0.5">
												{#each barFills(account, window) as fill}
													<div
														class="h-1 flex-1 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800"
													>
														<div
															class="h-full rounded-full {barColor(fill)}"
															style="width: {Math.min(100, Math.max(2, fill))}%"
														></div>
													</div>
												{/each}
											</div>
										</div>
									{/each}
								</div>
								{#if account.provider === 'codex' && account.limits?.reset_credits}
									<span
										class="flex shrink-0 items-center gap-0.5 rounded-full bg-blue-50 px-1.5 py-0.5 text-[0.625rem] font-medium text-blue-700 dark:bg-blue-950/40 dark:text-blue-300"
										title={couponTitle(account)}
									>
										<ArrowPath className="size-2.5" strokeWidth="2.5" />
										{account.limits.reset_credits}
									</span>
								{/if}
								<ChevronDown
									className="size-3.5 shrink-0 text-gray-400 transition-transform {expanded[
										account.id
									]
										? 'rotate-180'
										: ''}"
									strokeWidth="2"
								/>
							</button>

							{#if expanded[account.id]}
								<div class="space-y-3 px-3 pb-3 pt-1 text-xs">
									{#if account.last_error}
										<p class="text-amber-700 dark:text-amber-300">{account.last_error}</p>
									{/if}

									{#if windows.length}
										<div class="space-y-2">
											{#each windows as window}
												{@const split = splitOf(account, window)}
												<div>
													<div class="flex justify-between gap-2 text-gray-600 dark:text-gray-400">
														<span>{limitLabel(window)}</span>
														<span
															>{window.unit === 'usd'
																? amountText(window)
																: `${Math.round(window.used_percent)}% used`}{window.resets_at
																? ` · ${resetText(window.resets_at)}`
																: ''}</span
														>
													</div>
													<div class="mt-1 flex gap-1">
														{#each barFills(account, window) as fill}
															<div
																class="h-1.5 flex-1 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800"
															>
																<div
																	class="h-full rounded-full {barColor(fill)}"
																	style="width: {Math.min(100, Math.max(1, fill))}%"
																></div>
															</div>
														{/each}
													</div>
													{#if window.source === 'ledger'}
														<p class="mt-0.5 text-[0.625rem] text-gray-500">
															Estimated from token counts and published prices{window.unpriced
																? `; ${window.unpriced} ${window.unpriced === 1 ? 'request' : 'requests'} without a published price not counted`
																: ''}.
														</p>
													{/if}
													{#if split}
														<div
															class="mt-0.5 flex justify-between gap-2 text-[0.625rem] text-gray-500"
														>
															<span title="Half of each window is kept for the account's owner"
																>{ownerName(account)} · {Math.round(split.reserve)}/{RESERVE}</span
															>
															<span title="The other half is shared by the whole team"
																>Team · {Math.round(split.pool)}/{100 - RESERVE}</span
															>
														</div>
													{/if}
												</div>
											{/each}
										</div>
									{:else if !account.last_error}
										<p class="text-gray-500">No limits reported yet.</p>
									{/if}

									{#if account.usage?.members.length}
										<!-- Mologate: each member's use, counted in the usage ledger. -->
										<div>
											<div class="mb-1 text-gray-600 dark:text-gray-400">
												{account.usage.period === 'month' ? 'Use this month' : 'Use this week'}
											</div>
											<ul class="space-y-0.5 text-[0.6875rem] text-gray-500">
												{#each account.usage.members as member (member.user_id)}
													<li class="flex justify-between gap-2">
														<span class="truncate"
															>{member.user_id === $user?.id ? 'You' : member.name}</span
														>
														<span class="shrink-0 font-mono"
															>{member.requests}
															{member.requests === 1 ? 'request' : 'requests'} · {formatCount(
																member.input_tokens + member.output_tokens
															)} tokens{member.cost !== null
																? ` · ${formatDollars(member.cost)}`
																: ''}</span
														>
													</li>
												{/each}
											</ul>
										</div>
									{/if}

									{#if account.can_manage}
										<div class="flex flex-wrap items-center justify-between gap-2">
											<label class="flex items-center gap-2 text-gray-600 dark:text-gray-400"
												>Who can use
												<select
													value={account.sharing}
													disabled={busy}
													class="rounded-xl bg-gray-50 py-1 pl-2 pr-7 text-gray-900 outline-hidden dark:bg-gray-850 dark:text-white"
													on:change={(event) =>
														change(account, { sharing: event.currentTarget.value })}
												>
													{#each account.owner_user_id ? sharingOptions : teamOptions as option}
														<option value={option.value}>{option.label}</option>
													{/each}
												</select>
											</label>
											{#if $user?.role === 'admin' && account.provider === 'zai' && account.plan !== 'api'}
												<!-- Mologate: members pick their Z.ai coding plan; an admin can correct it. -->
												<label class="flex items-center gap-2 text-gray-600 dark:text-gray-400"
													>Plan
													<select
														value={account.plan}
														disabled={busy}
														class="rounded-xl bg-gray-50 py-1 pl-2 pr-7 text-gray-900 outline-hidden dark:bg-gray-850 dark:text-white"
														on:change={(event) =>
															change(account, { plan: event.currentTarget.value })}
													>
														{#each ['lite', 'pro', 'max'] as value}
															<option {value}>{planName(value)}</option>
														{/each}
													</select>
												</label>
											{/if}
											<div class="flex items-center gap-1.5">
												<button
													type="button"
													disabled={busy}
													class={buttonClass}
													on:click={() => verify(account)}>Verify</button
												>
												{#if account.budget}
													<button
														type="button"
														disabled={busy}
														class={buttonClass}
														on:click={() => {
															editingId = account.id;
															editedBudget = account.budget?.amount ?? 20;
														}}>${account.budget.amount}/mo</button
													>
												{/if}
												<button
													type="button"
													disabled={busy}
													class={dangerButtonClass}
													on:click={() => (disconnectId = account.id)}>Disconnect</button
												>
											</div>
										</div>
										{#if editingId === account.id}
											<form
												on:submit|preventDefault={() =>
													change(account, { budget: { amount: editedBudget, currency: 'USD' } })}
												class="flex items-center gap-2"
											>
												<span class="text-gray-500">Monthly budget $</span>
												<input
													type="number"
													aria-label="Monthly budget in USD"
													bind:value={editedBudget}
													min="0.01"
													max="1000000"
													step="0.01"
													required
													class="w-24 rounded-xl bg-gray-50 px-2 py-1 outline-hidden dark:bg-gray-850"
												/>
												<button type="submit" disabled={busy} class={buttonClass}>Save</button>
												<button type="button" class={buttonClass} on:click={() => (editingId = '')}
													>Cancel</button
												>
											</form>
										{/if}
										{#if disconnectId === account.id}
											<div
												class="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-red-50 px-3 py-2 dark:bg-red-950/30"
											>
												<span class="text-red-700 dark:text-red-300"
													>Disconnect {account.label}? Requests stop using it.</span
												>
												<div class="flex gap-1.5">
													<button
														type="button"
														disabled={busy}
														class="rounded-full bg-red-600 px-3 py-1 font-medium text-white transition hover:bg-red-700 disabled:opacity-50"
														on:click={() => disconnect(account)}>Disconnect</button
													>
													<button
														type="button"
														class={buttonClass}
														on:click={() => (disconnectId = '')}>Keep</button
													>
												</div>
											</div>
										{/if}
									{/if}
								</div>
							{/if}
						</li>
					{/each}
				</ul>
			{/if}
		</section>

		<!-- Mologate: the member's own use, read-only (docs/limits-and-accounts.md §8). Shares of an
		     account window appear once metering has calibrated the provider. -->
		{#if myUsage && myUsage.accounts.length}
			<section class="mt-4">
				<h3 class="mb-2 text-xs text-gray-600 dark:text-gray-400">Your use · last 7 days</h3>
				<ul
					class="divide-y divide-gray-100 rounded-2xl border border-gray-100 text-xs dark:divide-gray-850 dark:border-gray-850"
				>
					{#each myUsage.accounts as entry (entry.id)}
						<li class="px-3 py-2">
							<div class="flex items-baseline justify-between gap-3">
								<span class="truncate font-medium text-gray-900 dark:text-white">{entry.label}</span
								>
								<span class="shrink-0 text-[0.6875rem] text-gray-500">
									{entry.requests} requests · {formatCount(entry.input_tokens)} in · {formatCount(
										entry.output_tokens
									)} out{entry.cost != null ? ` · ${formatDollars(entry.cost)}` : ''}
								</span>
							</div>
							{#if entry.windows.length}
								<div class="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-[0.6875rem] text-gray-500">
									{#each entry.windows as item}
										<span
											title={item.calibrated
												? 'Your share of this account window since it last reset, estimated from your tokens.'
												: 'Metering learns what a token costs on this provider from about a week of use.'}
										>
											{item.window === '5h' ? '5-hour' : 'Weekly'}:
											{item.percent != null
												? `you ${formatPercent(item.percent)}`
												: 'measuring'}{item.outside_percent
												? ` · outside Mologate ${formatPercent(item.outside_percent)}`
												: ''}
										</span>
									{/each}
								</div>
							{/if}
						</li>
					{/each}
				</ul>
			</section>
		{/if}
	</div>
</div>
