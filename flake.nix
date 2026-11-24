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

        # Output and browser profile go in the current directory unless overridden.
        mkApp = name: script: envVar: pkgs.writeShellApplication {
          inherit name;
          runtimeInputs = [ python ];
          runtimeEnv = playwrightEnv;
          text = ''
            export ${envVar}="''${${envVar}:-$PWD}"
            exec python ${script} "$@"
          '';
        };

        aliexpress = mkApp "aliexpress-orders" ./aliexpress_orders.py "ALIEXPRESS_ORDERS_DIR";
        tinytronics = mkApp "tinytronics-orders" ./tinytronics_orders.py "TINYTRONICS_ORDERS_DIR";
        conrad = mkApp "conrad-orders" ./conrad_orders.py "CONRAD_ORDERS_DIR";
        automation24 = mkApp "automation24-orders" ./automation24_orders.py "AUTOMATION24_ORDERS_DIR";
        reichelt = mkApp "reichelt-orders" ./reichelt_orders.py "REICHELT_ORDERS_DIR";
        solyd = mkApp "solyd-orders" ./solyd_orders.py "SOLYD_ORDERS_DIR";
      in
      {
        packages.default = aliexpress;
        packages.aliexpress = aliexpress;
        packages.tinytronics = tinytronics;
        packages.conrad = conrad;
        packages.automation24 = automation24;
        packages.reichelt = reichelt;
        packages.solyd = solyd;

        apps.default = { type = "app"; program = "${aliexpress}/bin/aliexpress-orders"; };
        apps.aliexpress = { type = "app"; program = "${aliexpress}/bin/aliexpress-orders"; };
        apps.tinytronics = { type = "app"; program = "${tinytronics}/bin/tinytronics-orders"; };
        apps.conrad = { type = "app"; program = "${conrad}/bin/conrad-orders"; };
        apps.automation24 = { type = "app"; program = "${automation24}/bin/automation24-orders"; };
        apps.reichelt = { type = "app"; program = "${reichelt}/bin/reichelt-orders"; };
        apps.solyd = { type = "app"; program = "${solyd}/bin/solyd-orders"; };

        devShells.default = pkgs.mkShell ({
          packages = [ python ];
        } // playwrightEnv);
      });
}
