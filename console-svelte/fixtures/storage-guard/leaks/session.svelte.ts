export const session = $state({ token: sessionStorage.getItem("token") ?? "" });
