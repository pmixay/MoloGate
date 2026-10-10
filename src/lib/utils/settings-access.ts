import type { SettingsAccessContext } from './settings-search';

export const canEditSystemPrompt = ({ user }: SettingsAccessContext) =>
	user?.role === 'admin' ||
	((user?.permissions?.chat?.controls ?? true) && (user?.permissions?.chat?.system_prompt ?? true));
export const canEditParameters = ({ user }: SettingsAccessContext) =>
	user?.role === 'admin' ||
	((user?.permissions?.chat?.controls ?? true) && (user?.permissions?.chat?.params ?? true));
export const canManageChats = (
	{ user }: SettingsAccessContext,
	action: 'import' | 'export' | 'delete'
) => user?.role === 'admin' || (user?.permissions?.chat?.[action] ?? true);
export const canUseApiKeys = ({ user, config }: SettingsAccessContext) =>
	(config?.features?.enable_api_keys ?? true) &&
	(user?.role === 'admin' || (user?.permissions?.features?.api_keys ?? false));
export const canUseNotificationTargets = ({ user, config }: SettingsAccessContext) =>
	config?.features?.enable_user_webhooks &&
	(user?.role === 'admin' || (user?.permissions?.features?.webhooks ?? false));
export const canUseTemporaryChats = ({ user }: SettingsAccessContext) =>
	user?.role === 'admin' || user?.permissions?.chat?.temporary;
export const isSettingsAdmin = ({ user }: SettingsAccessContext) => user?.role === 'admin';

// Mologate: settings removed from the interface, so search must not point at them.
const REMOVED_SETTING_STEMS = new Set([
	'settings.personal.general.sections.systemPrompt',
	'settings.personal.account.bio',
	'settings.personal.account.gender',
	'settings.personal.account.birthDate',
	'settings.personal.about.seeWhatSNew',
	'settings.admin.general.seeWhatSNew',
	'settings.personal.shortcuts.toggleDictation',
	'settings.personal.shortcuts.toggleControls',
	'settings.personal.shortcuts.toggleMute'
]);

/** Only permission-gated text needs an explicit rule. Provider/disclosure state is irrelevant. */
export function canSearchSetting(
	key: string,
	tabId: string,
	context: SettingsAccessContext
): boolean {
	const stem = key.replace(/\.(label|description|title)$/, '');
	if (REMOVED_SETTING_STEMS.has(stem)) return false;
	if (stem === 'settings.personal.general.sections.systemPrompt')
		return canEditSystemPrompt(context);
	if (
		stem === 'settings.personal.general.sections.advancedParameters' ||
		stem === 'settings.personal.general.modelParameters' ||
		(key.startsWith('settings.personal.general.parameters.') && tabId === 'general')
	) {
		if (!canEditParameters(context)) return false;
	}
	if (
		key.startsWith('settings.personal.general.parameters.') &&
		/\.(streamDeltaChunkSize|contextCompactionThreshold|numThread|numGpu|useMmap|useMlock|keepAlive)\./.test(
			key
		)
	)
		return isSettingsAdmin(context);
	if (key.startsWith('settings.personal.dataControls.')) {
		if (stem.endsWith('.importChats')) return canManageChats(context, 'import');
		if (stem.endsWith('.exportChats')) return canManageChats(context, 'export');
		if (/\.(deleteAllChats|deleteAll)$/.test(stem)) return canManageChats(context, 'delete');
	}
	if (
		key.startsWith('settings.personal.notifications.') &&
		/\.(notificationTargets|addNotificationTarget)\./.test(key)
	)
		return !!canUseNotificationTargets(context);
	if (
		key.startsWith('settings.personal.account.') &&
		/\.(apiKeys|secrets|jwtToken|copyToken|apiKey|copyApiKey|createNewSecretKey)\./.test(key)
	) {
		return (
			canUseApiKeys(context) && (!/\.(jwtToken|copyToken)\./.test(key) || isSettingsAdmin(context))
		);
	}
	if (stem === 'settings.personal.interface.toastNotificationsForNewUpdates')
		return isSettingsAdmin(context);
	if (stem === 'settings.personal.interface.temporaryChatByDefault')
		return !!canUseTemporaryChats(context);
	return true;
}
