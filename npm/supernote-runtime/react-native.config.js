'use strict';

module.exports = {
  dependency: {
    platforms: {
      android: {
        sourceDir: './android',
        packageImportPath:
          'import supernote.generated.runtime.SupernoteModulePackage;',
        packageInstance: 'new SupernoteModulePackage()',
      },
    },
  },
};
