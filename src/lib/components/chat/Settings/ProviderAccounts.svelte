<script lang="ts">
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { getModels } from '$lib/apis';
	import { models, settings, config, user } from '$lib/stores';
	import {
		providerAccountRequest,
		type AutoReset,
		type ProviderAccount,
		type DeviceLogin,
		type Sharing
	} from '$lib/apis/provider-accounts';

	const providers: Record<string, string> = {
		codex: 'ChatGPT / Codex',
		anthropic_api: 'Anthropic API',
		openai_api: 'OpenAI API',
		openrouter: 'OpenRouter',
		zai: 'Z.ai'
	};
	let accounts: ProviderAccount[] = [];
	let autoReset: AutoReset | null = null;
	let loading = true;
	let busy = false;
	let error = '';
	let provider = 'codex';
	let label = '';
	let apiKey = '';
	let budget = 20;
	let plan = 'api';
	let sharing: Sharing = 'private';
	let login: DeviceLogin | null = null;
	let loginMethod: 'link' | 'device_code' = 'link';
	let callbackUrl = '';
	let disconnectId = '';
	let editingId = '';
	let editedBudget = 20;
	let active = true;
	let polling = false;

	const request = <T,>(path = '', method = 'GET', body?: object) =>
		providerAccountRequest<T>(localStorage.token, path, method, body);
	const message = (err: unknown) =>
		err instanceof Error ? err.message : 'The account request failed.';
	const refresh = async (refreshModels = false) => {
		[accounts, autoReset] = await Promise.all([
			request<{ data: ProviderAccount[] }>().then((result) => result.data),
			request<AutoReset>('/auto-reset')
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
		try {
			const next = await request<DeviceLogin>(`/codex/logins/${login.id}`);
			if (!active) return;
			await applyLogin(next);
		} catch (err) {
			error = message(err);
		} finally {
			polling = false;
		}
	};
	const applyLogin = async (next: DeviceLogin) => {
		login = next;
		if (login.status === 'completed') {
			localStorage.removeItem(`mologate-device-login:${$user?.id}`);
			callbackUrl = '';
			await refresh(true);
			toast.success('ChatGPT account connected. Its models are ready to use.');
		} else if (login.status === 'error') {
			localStorage.removeItem(`mologate-device-login:${$user?.id}`);
			callbackUrl = '';
			error = login.error || 'Sign-in expired. Start a new connection.';
		}
	};
	// Mologate: the browser's failed 127.0.0.1 page carries the sign-in result back to Mologate.
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
			error = message(err);
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
			localStorage.removeItem(`mologate-device-login:${$user?.id}`);
			login = null;
			callbackUrl = '';
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	};
	const connect = async () => {
		busy = true;
		error = '';
		try {
			if (provider === 'codex') {
				login = await request<DeviceLogin>('/codex', 'POST', {
					label: label.trim() || 'ChatGPT',
					sharing,
					method: loginMethod
				});
				localStorage.setItem(`mologate-device-login:${$user?.id}`, login.id);
			} else {
				await request('', 'POST', {
					provider,
					label: label.trim() || providers[provider],
					api_key: apiKey,
					plan: provider === 'zai' ? plan : 'api',
					sharing,
					budget: provider === 'zai' && plan !== 'api' ? null : { amount: budget, currency: 'USD' }
				});
				apiKey = '';
				label = '';
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
			toast.success(enabled ? 'Automatic limit resets are on.' : 'Automatic limit resets are off.');
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

<div class="space-y-5 text-sm pb-4">
	<div>
		<h2 class="font-semibold">Provider accounts</h2>
		<p class="mt-1 text-gray-500">
			Connect an account to use its models. Sharing is optional and starts private.
		</p>
	</div>
	{#if error}<p
			role="alert"
			class="rounded-lg bg-red-50 dark:bg-red-950/40 p-3 text-red-700 dark:text-red-300"
		>
			{error}
		</p>{/if}
	<form
		on:submit|preventDefault={connect}
		class="space-y-3 rounded-xl border border-gray-200 dark:border-gray-800 p-4"
	>
		<h3 class="font-medium">Connect account</h3>
		<label class="block"
			>Provider
			<select
				bind:value={provider}
				disabled={busy}
				class="mt-1 w-full rounded-lg bg-gray-100 dark:bg-gray-850 p-2"
			>
				{#each Object.entries(providers) as [id, name]}<option value={id}>{name}</option>{/each}
			</select>
		</label>
		<label class="block"
			>Label
			<input
				bind:value={label}
				maxlength="100"
				placeholder="My account"
				class="mt-1 w-full rounded-lg bg-gray-100 dark:bg-gray-850 p-2"
			/>
		</label>
		{#if provider === 'zai'}
			<label class="block"
				>Plan
				<select bind:value={plan} class="mt-1 w-full rounded-lg bg-gray-100 dark:bg-gray-850 p-2">
					<option value="api">API</option><option value="lite">Coding Lite</option><option
						value="pro">Coding Pro</option
					><option value="max">Coding Max</option>
				</select>
			</label>
		{/if}
		{#if provider !== 'codex'}
			<label class="block"
				>API key
				<input
					bind:value={apiKey}
					type="password"
					required
					autocomplete="off"
					spellcheck="false"
					class="mt-1 w-full rounded-lg bg-gray-100 dark:bg-gray-850 p-2"
				/>
			</label>
			{#if provider !== 'zai' || plan === 'api'}
				<label class="block"
					>Monthly budget (USD)
					<input
						bind:value={budget}
						type="number"
						min="0.01"
						max="1000000"
						step="0.01"
						required
						class="mt-1 w-full rounded-lg bg-gray-100 dark:bg-gray-850 p-2"
					/>
				</label>
				<p class="text-xs text-gray-500">
					Budget is saved for future limits. Spending enforcement is not active yet.
				</p>
			{/if}
		{/if}
		{#if provider === 'codex'}
			<label class="block"
				>Sign-in method
				<select
					bind:value={loginMethod}
					class="mt-1 w-full rounded-lg bg-gray-100 dark:bg-gray-850 p-2"
				>
					<option value="link">Link — sign in, then paste the address back</option><option
						value="device_code">Code — enter a code on OpenAI's device page</option
					>
				</select>
			</label>
		{/if}
		<label class="block"
			>Sharing
			<select bind:value={sharing} class="mt-1 w-full rounded-lg bg-gray-100 dark:bg-gray-850 p-2">
				<option value="private">Private — only me</option><option value="shared"
					>Shared — approved team members</option
				>
			</select>
		</label>
		<button
			type="submit"
			disabled={busy || (provider === 'codex' && login?.status === 'pending')}
			class="rounded-lg bg-black dark:bg-white text-white dark:text-black px-4 py-2 disabled:opacity-50"
		>
			{busy ? 'Connecting…' : provider === 'codex' ? 'Connect ChatGPT' : 'Verify and connect'}
		</button>
	</form>
	{#if login?.status === 'pending'}
		<div class="rounded-xl border border-blue-300 dark:border-blue-800 p-4 space-y-2" role="status">
			<h3 class="font-medium">Finish your ChatGPT sign-in</h3>
			{#if login.method === 'link' && login.auth_url}
				<ol class="list-decimal ml-5 space-y-1">
					<li>
						<a
							class="text-blue-600 dark:text-blue-400 underline"
							href={login.auth_url}
							target="_blank"
							rel="noopener noreferrer">Open the OpenAI sign-in link</a
						> and sign in.
					</li>
					<li>
						Your browser then opens a page at <span class="font-mono">127.0.0.1:1455</span> that does
						not load. That is expected.
					</li>
					<li>Copy the full address from the address bar and paste it here.</li>
				</ol>
				<form on:submit|preventDefault={finishLink} class="space-y-2">
					<input
						bind:value={callbackUrl}
						aria-label="Address after signing in"
						placeholder="http://127.0.0.1:1455/auth/callback?code=…"
						required
						autocomplete="off"
						spellcheck="false"
						class="w-full rounded-lg bg-gray-100 dark:bg-gray-850 p-2 font-mono text-xs"
					/>
					<button
						type="submit"
						disabled={busy || !callbackUrl.trim()}
						class="rounded-lg bg-black dark:bg-white text-white dark:text-black px-4 py-2 disabled:opacity-50"
						>{busy ? 'Finishing…' : 'Finish sign-in'}</button
					>
				</form>
				<p class="text-xs text-gray-500">
					Expires at {new Date(login.expires_at * 1000).toLocaleTimeString()}.
				</p>
			{:else if login.user_code && login.verification_url}
				<p>Open the sign-in page and enter this code:</p>
				<p class="font-mono text-xl tracking-widest select-all">{login.user_code}</p>
				<a
					class="text-blue-600 dark:text-blue-400 underline"
					href={login.verification_url}
					target="_blank"
					rel="noopener noreferrer">Open OpenAI sign-in</a
				>
				<p class="text-xs text-gray-500">
					Waiting for sign-in. Expires at {new Date(login.expires_at * 1000).toLocaleTimeString()}.
				</p>
			{:else}<p>Loading sign-in details…</p>{/if}
			<button disabled={busy} on:click={cancelLogin} class="text-xs underline"
				>Cancel sign-in</button
			>
		</div>
	{/if}
	<div class="space-y-3">
		<h3 class="font-medium">Connected accounts</h3>
		{#if autoReset}
			<div class="rounded-xl border border-gray-200 dark:border-gray-800 p-4 space-y-1">
				<label class="flex gap-2 items-center font-medium"
					><input
						type="checkbox"
						checked={autoReset.enabled}
						disabled={busy}
						on:change={(event) => setAutoReset(event.currentTarget.checked)}
					/>Automatic limit resets</label
				>
				<p class="text-xs text-gray-500">
					When every ChatGPT account that can serve a model is out of quota, Mologate redeems the
					reset coupon that expires soonest on one of them. This applies to the whole team, and any
					member can turn it off.
				</p>
				{#if autoReset.changed_by && autoReset.changed_at}<p class="text-xs text-gray-500">
						Turned {autoReset.enabled ? 'on' : 'off'} by {autoReset.changed_by} on {new Date(
							autoReset.changed_at * 1000
						).toLocaleString()}.
					</p>{/if}
			</div>
		{/if}
		{#if loading}<p class="text-gray-500">Loading accounts…</p>
		{:else if accounts.length === 0}<p class="text-gray-500">No accounts connected yet.</p>{/if}
		{#each accounts as account (account.id)}
			<div class="rounded-xl border border-gray-200 dark:border-gray-800 p-4 space-y-3">
				<div class="flex items-start justify-between gap-3">
					<div>
						<h4 class="font-medium break-all">{account.label}</h4>
						<p class="text-xs text-gray-500">
							{providers[account.provider]} · {account.plan} · {account.owner_name}
						</p>
					</div>
					<span class="text-xs rounded-full px-2 py-1 bg-gray-100 dark:bg-gray-850"
						>{account.sharing === 'paused'
							? 'Paused'
							: account.status === 'ok'
								? 'Connected'
								: account.status === 'needs_login'
									? 'Sign-in required'
									: account.status}</span
					>
				</div>
				{#if account.last_error}<p class="text-amber-700 dark:text-amber-300">
						{account.last_error}
					</p>{/if}
				<p class="text-xs text-gray-500">
					{account.models.length} models · {account.sharing}{account.budget
						? ` · $${account.budget.amount}/month budget`
						: ''}
				</p>
				{#each account.limits?.windows ?? [] as window}
					<div>
						<div class="flex justify-between text-xs mb-1">
							<span
								>{window.window_minutes
									? `${window.window_minutes / 60}-hour window`
									: window.id}</span
							><span>{Math.round(window.used_percent)}% used</span>
						</div>
						<progress
							class="w-full h-2"
							value={window.used_percent}
							max="100"
							aria-label={`${window.id} usage`}
						></progress>
						{#if window.resets_at}<p class="text-xs text-gray-500">
								Resets {new Date(window.resets_at * 1000).toLocaleString()}
							</p>{/if}
					</div>
				{:else}<p class="text-xs text-gray-500">No provider limits reported yet.</p>{/each}
				{#if account.provider === 'codex' && account.limits?.reset_credits != null}<p
						class="text-xs text-gray-500"
					>
						{account.limits.reset_credits === 1
							? '1 reset coupon available'
							: `${account.limits.reset_credits} reset coupons available`}{account.allow_team_resets
							? ''
							: ' · automatic resets not allowed'}
					</p>{/if}
				{#if account.last_auto_reset_at}<p class="text-xs text-gray-500">
						Last automatic reset {new Date(account.last_auto_reset_at * 1000).toLocaleString()}
					</p>{/if}
				{#if account.can_manage}
					<label class="block text-xs"
						>Sharing for {account.label}
						<select
							value={account.sharing}
							disabled={busy}
							on:change={(event) => change(account, { sharing: event.currentTarget.value })}
							class="mt-1 w-full rounded-lg bg-gray-100 dark:bg-gray-850 p-2 text-sm"
						>
							<option value="private">Private — owner only</option><option value="shared"
								>Shared — approved team members</option
							><option value="paused">Paused — unavailable to everyone</option>
						</select>
					</label>
					{#if account.provider === 'codex'}
						<label class="flex gap-2 items-center text-xs"
							><input
								type="checkbox"
								checked={account.allow_team_resets}
								disabled={busy}
								on:change={(event) =>
									change(account, { allow_team_resets: event.currentTarget.checked })}
							/>Allow automatic resets with this account's coupons</label
						>
					{/if}
					<div class="flex gap-4 text-xs">
						<button disabled={busy} on:click={() => verify(account)} class="underline"
							>Verify</button
						>
						{#if account.budget}<button
								disabled={busy}
								on:click={() => {
									editingId = account.id;
									editedBudget = account.budget?.amount ?? 20;
								}}
								class="underline">Edit budget</button
							>{/if}
						<button
							disabled={busy}
							on:click={() => {
								disconnectId = account.id;
							}}
							class="text-red-600 dark:text-red-400 underline">Disconnect</button
						>
					</div>
					{#if editingId === account.id}
						<form
							on:submit|preventDefault={() =>
								change(account, { budget: { amount: editedBudget, currency: 'USD' } })}
							class="flex gap-2 items-center"
						>
							<input
								type="number"
								aria-label="Monthly budget in USD"
								bind:value={editedBudget}
								min="0.01"
								max="1000000"
								step="0.01"
								required
								class="w-28 rounded-lg bg-gray-100 dark:bg-gray-850 p-2"
							/>
							<button type="submit" disabled={busy}>Save</button><button
								type="button"
								on:click={() => {
									editingId = '';
								}}>Cancel</button
							>
						</form>
					{/if}
					{#if disconnectId === account.id}
						<div class="text-xs space-y-2">
							<p>Disconnect {account.label}? Requests will stop using this account.</p>
							<div class="flex gap-4">
								<button
									disabled={busy}
									on:click={() => disconnect(account)}
									class="text-red-600 dark:text-red-400 underline">Confirm disconnect</button
								><button
									on:click={() => {
										disconnectId = '';
									}}>Cancel</button
								>
							</div>
						</div>
					{/if}
				{/if}
			</div>
		{/each}
	</div>
</div>
