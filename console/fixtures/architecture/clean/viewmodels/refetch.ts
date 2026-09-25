export const refetch = (api: { status: () => Promise<unknown> }) => api.status();
