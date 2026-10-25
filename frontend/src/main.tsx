import "@fontsource-variable/inter";
import "@fontsource-variable/jetbrains-mono";
import "./index.css";
import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Toaster } from "sonner";
import { ApiError } from "@/api/client";
import { AuthProvider } from "@/lib/auth";
import { FEDERATION_DISCONNECTED, federationCode, noteFederationError, noteSuccess } from "@/lib/federation";
import { ConfirmProvider, TooltipProvider } from "@/components/ui";
import App from "./App";

const queryClient = new QueryClient({
  queryCache: new QueryCache({ onError: noteFederationError, onSuccess: noteSuccess }),
  mutationCache: new MutationCache({ onError: noteFederationError }),
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      refetchOnWindowFocus: true,
      retry: (count, error) =>
        !(error instanceof ApiError && error.status >= 400 && error.status < 500) &&
        federationCode(error) !== FEDERATION_DISCONNECTED &&
        count < 2,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <TooltipProvider>
          <ConfirmProvider>
            <App />
            <Toaster
              position="bottom-right"
              toastOptions={{
                className: "!bg-raised !text-fg !border-border !shadow-pop !rounded-xl !text-[13px]",
              }}
            />
          </ConfirmProvider>
        </TooltipProvider>
      </AuthProvider>
    </QueryClientProvider>
  </StrictMode>,
);
