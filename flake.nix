{
  description = "Sync Markdown files with Google Docs — create, push, pull, comment round-trip, and opinionated styling";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: rec {
        gdoc-sync = pkgs.python3Packages.buildPythonApplication {
          pname = "gdoc-sync";
          version = "0.5.1";
          pyproject = true;
          src = ./.;

          build-system = [ pkgs.python3Packages.setuptools ];
          nativeBuildInputs = [ pkgs.makeWrapper ];

          propagatedBuildInputs = with pkgs.python3Packages; [
            google-api-python-client
            google-auth-oauthlib
            google-auth-httplib2
            pyyaml
          ];

          # pandoc does the markdown → docx conversion at runtime.
          postFixup = ''
            wrapProgram $out/bin/gdoc-sync \
              --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.pandoc ]}
          '';

          nativeCheckInputs = [ pkgs.python3Packages.pytestCheckHook ];

          meta = {
            description = "Sync Markdown files with Google Docs from the CLI";
            homepage = "https://github.com/MattHandzel/gdoc-sync";
            license = pkgs.lib.licenses.mit;
            mainProgram = "gdoc-sync";
          };
        };
        default = gdoc-sync;
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: with ps; [
              google-api-python-client
              google-auth-oauthlib
              google-auth-httplib2
              pyyaml
              pytest
            ]))
            pkgs.pandoc
            pkgs.ruff
          ];
        };
      });
    };
}
