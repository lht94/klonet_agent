import os
import posixpath
from flask_uploads import UploadSet, UploadNotAllowed
from werkzeug.utils import secure_filename
from werkzeug.datastructures import FileStorage

def extension(filename):
    ext = os.path.splitext(filename)[1]
    if ext.startswith('.'):
        # os.path.splitext retains . separator
        ext = ext[1:]
    return ext

class VemuUploadSet(UploadSet):
    def save(self, storage, folder=None, name=None):
        """
        This saves a `werkzeug.FileStorage` into this upload set. If the
        upload is not allowed, an `UploadNotAllowed` error will be raised.
        Otherwise, the file will be saved and its name (including the folder)
        will be returned.

        :param storage: The uploaded file to save.
        :param folder: The subfolder within the upload set to save to.
        :param name: The name to save the file as. If it ends with a dot, the
                     file's extension will be appended to the end. (If you
                     are using `name`, you can include the folder in the
                     `name` instead of explicitly using `folder`, i.e.
                     ``uset.save(file, name="someguy/photo_123.")``
        """
        if not isinstance(storage, FileStorage):
            raise TypeError("storage must be a werkzeug.FileStorage")

        if folder is None and name is not None and "/" in name:
            folder, name = os.path.split(name)

        basename = self.get_basename(storage.filename)
        if name:
            if name.endswith('.'):
                basename = name + extension(basename)
            else:
                basename = name

        if not self.file_allowed(storage, basename):
            raise UploadNotAllowed(f"format error: {basename}, allowed format: "
                f"{self.config.allow + self.extensions}")

        if folder:
            target_folder = os.path.join(self.config.destination, folder)
        else:
            target_folder = self.config.destination
        if not os.path.exists(target_folder):
            os.makedirs(target_folder)
        if os.path.exists(os.path.join(target_folder, basename)):
            basename = self.resolve_conflict(target_folder, basename)

        target = os.path.join(target_folder, basename)
        storage.save(target)
        if folder:
            return posixpath.join(folder, basename)
        else:
            return basename