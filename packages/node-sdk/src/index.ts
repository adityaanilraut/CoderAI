export { CoderaiHarness } from '#/coderai-harness';
export type { CoderaiHarnessRuntimeOptions } from '#/coderai-harness';
export { Session } from '#/session';
export { KimiAuthFacade } from '#/auth';
export {
  createCoderaiHarness,
  SDKRpcClientV2,
  type SDKRpcClientV2Options,
} from '#/sdk-rpc-client-v2';
export {
  createKimiConfigRpc,
  KimiConfigRpcClient,
  type KimiConfigRpc,
  type KimiConfigValidationIssue,
  type KimiConfigValidationPathSegment,
  type ResolveKimiConfigPathInput,
  type ValidateKimiConfigTomlInput,
} from '#/config-rpc';
export { SDKRpcClientBase } from '#/rpc';
export type { ConfigSectionChange } from '#/rpc';
export { KimiForCodingProvider } from '#/kimi-code-model-provider';
export type { KimiForCodingProviderOptions } from '#/kimi-code-model-provider';

export {
  applyCatalogProvider,
  catalogBaseUrl,
  catalogModelToAlias,
  catalogProviderModels,
  CatalogFetchError,
  RegistryImportError,
  DEFAULT_CATALOG_URL,
  fetchCatalog,
  inferWireType,
  loadBuiltInCatalog,
  resolveCatalogImport,
} from '#/catalog';
export type {
  ApplyCatalogProviderOptions,
  Catalog,
  CatalogImportInvalidReason,
  CatalogImportResolution,
  CatalogModel,
  CatalogProviderEntry,
  FetchCatalogOptions,
} from '#/catalog';

export {
  ErrorCodes,
  KimiError,
  type KimiErrorCode,
  type KimiErrorInfo,
  type KimiErrorOptions,
  type KimiErrorPayload,
  KIMI_ERROR_INFO,
  fromKimiErrorPayload,
  isKimiError,
  toKimiErrorPayload,
} from '#/errors';

export {
  flushDiagnosticLogs,
  flushDiagnosticLogsSync,
  log,
  redact,
  resolveGlobalLogPath,
} from '#/logging/index';
export { resolveKimiHome } from '@coderai/agent-core-v2';
export type { LogContext, LogLevel, LogPayload, Logger } from '#/logging/index';

export { effectiveModelAlias, loadRuntimeConfigSafe } from '#/config/index';
export { resolveConfigPath } from '@coderai/agent-core-v2';
export { limitAgentReplayByTurns } from '#/replay';
export { parseAgentFileText, resolveAgentPath } from '@coderai/agent-core-v2';
export { SECONDARY_DERIVED_MODEL_ALIAS } from '#/config/index';
export { PRIMARY_SUBAGENT_MODEL_CHOICE } from '@coderai/agent-core-v2/session/subagent/configSection';

export { installGlobalProxyDispatcher } from '#/proxy';

export {
  buildImageCompressionCaption,
  buildUnsupportedImageNotice,
  gateImageFormatParts,
  isModelAcceptedImageMime,
  normalizeImageMime,
  parseImageDataUrl,
  persistOriginalImage,
  sessionMediaOriginalsDir,
  IMAGE_BYTE_BUDGET,
  MAX_IMAGE_EDGE_PX,
} from '@coderai/agent-core-v2';
export { compressBase64ForModel, compressImageForModel, ImageLimits } from '#/image';
export type {
  CompressImageOptions,
  CompressImageResult,
  CompressBase64Result,
  ImageCompressionCaptionInput,
  ImageCompressionTelemetry,
} from '#/image';

export type {
  ExperimentalFeatureState,
  ExperimentalFlagMap,
  ExperimentalFlagSource,
  FlagDefinition,
  FlagDefinitionInput,
  FlagId,
  FlagSurface,
} from '#/flag';

export {
  mediaKindForPath,
  buildDaemonFileUrl,
  buildMediaPathTag,
  isDaemonFileUrl,
  matchSingleMediaPathTag,
  parseDaemonFileUrl,
} from '@coderai/agent-core-v2/agent/media/mediaRef';
export type {
  DaemonFileRef,
  MediaKind,
} from '@coderai/agent-core-v2/agent/media/mediaRef';

export type {
  KimiAuthCompleteFeedbackUploadInput,
  KimiAuthCompleteFeedbackUploadPart,
  KimiAuthCreateFeedbackUploadUrlInput,
  KimiAuthCreateFeedbackUploadUrlOk,
  KimiAuthCreateFeedbackUploadUrlResult,
  KimiAuthFeedbackUploadPart,
  KimiAuthLoginResult,
  KimiAuthLogoutResult,
  KimiAuthSubmitFeedbackInput,
} from '#/auth';

export * from '#/events';
export type * from '#/types';

export { configureTextBuildWorkerRuntime } from '@coderai/minidb/worker-runtime';

// Compatibility aliases for integrations using the original SDK names.
export { CoderaiHarness as KimiHarness } from '#/coderai-harness';
export type { CoderaiHarnessRuntimeOptions as KimiHarnessRuntimeOptions } from '#/coderai-harness';
export { createCoderaiHarness as createKimiHarness } from '#/sdk-rpc-client-v2';

export { detectFileType, MEDIA_SNIFF_BYTES } from '@coderai/agent-core-v2/agent/media/file-type';
export { McpServerConfigSchema } from '@coderai/agent-core-v2/mcpCore/config-schema';
export type { UserToolRegistration, UserToolHandler, ToolCallRequest, ToolCallResponse } from '#/interaction';

export type { TurnChanges, TurnFileDiff } from '#/types';
