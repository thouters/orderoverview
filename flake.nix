{
  description = "AliExpress order overview: orders, product page screenshots and images";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        # Python playwright and the browsers come from the same nixpkgs, so their versions match.
        python = pkgs.python3.withPackages (ps: [ ps.playwright ps.pillow ]);

        playwrightEnv = {
          PLAYWRIGHT_BROWSERS_PATH = "${pkgs.playwright-driver.browsers}";
          PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
        };

        app = pkgs.writeShellApplication {
          name = "aliexpress-orders";
          runtimeInputs = [ python ];
          runtimeEnv = playwrightEnv;
          text = ''
            # Output and browser profile go in the current directory unless overridden.
            export ALIEXPRESS_ORDERS_DIR="''${ALIEXPRESS_ORDERS_DIR:-$PWD}"
            exec python ${./aliexpress_orders.py} "$@"
          '';
        };
      in
      {
        packages.default = app;

        apps.default = {
          type = "app";
          program = "${app}/bin/aliexpress-orders";
        };

        devShells.default = pkgs.mkShell ({
          packages = [ python ];
        } // playwrightEnv);
      });
}
