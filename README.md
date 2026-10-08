# todo

GitOps repo for todo

GitOps repo for the **todo** application (platform shape `gitops-app`). See `CLAUDE.md` for layout, pin policy and commands.

```bash
devbox shell
devbox run cluster-up              # local k3d cluster + ArgoCD + credentials + root Application
# real cluster, by a human: KUBE_CONTEXT=<ctx> devbox run bootstrap
/gitops:compose add my-api my-web     # in Claude Code: declare services
devbox run quality                    # validate overlays + ApplicationSets
/gitops:promote my-api dev staging    # pin staging to the dev image
```

Updates to this skeleton: `/shared:update-service`.
