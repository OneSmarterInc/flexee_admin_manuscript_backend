"""Private bounded document parser. Invoked by document_processing, never as an API."""
import os
import sys
from pathlib import Path


def limit_memory(megabytes):
    limit = megabytes * 1024 * 1024
    if os.name != 'nt':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        return None
    import ctypes
    from ctypes import wintypes as w
    size = ctypes.c_size_t
    class Basic(ctypes.Structure):
        _fields_ = [('per_process', ctypes.c_int64), ('per_job', ctypes.c_int64),
                    ('flags', w.DWORD), ('minimum', size), ('maximum', size),
                    ('active', w.DWORD), ('affinity', size), ('priority', w.DWORD), ('scheduling', w.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(key, ctypes.c_uint64) for key in ('read', 'write', 'other', 'read_bytes', 'write_bytes', 'other_bytes')]
    class Extended(ctypes.Structure):
        _fields_ = [('basic', Basic), ('io', IO), ('process_memory', size), ('job_memory', size),
                    ('peak_process', size), ('peak_job', size)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.SetInformationJobObject.restype = w.BOOL
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.AssignProcessToJobObject.restype = w.BOOL
    job = kernel.CreateJobObjectW(None, None)
    limits = Extended()
    limits.basic.flags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
    limits.process_memory = limit
    if (not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess())):
        raise RuntimeError('Cannot enforce parser memory limit')
    return job  # Keep the job handle alive until this process exits.


def main():
    source, destination, memory, max_pages, max_chars = sys.argv[1:]
    handle = limit_memory(int(memory))
    maximum = int(max_chars)
    suffix = Path(source).suffix.lower()
    if suffix == '.docx':
        from docx import Document
        pieces = (p.text for p in Document(source).paragraphs)
    elif suffix == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(source)
        if len(reader.pages) > int(max_pages):
            raise ValueError('Document exceeds page limit')
        pieces = ((p.extract_text() or '') for p in reader.pages)
    else:
        raise ValueError('Unsupported parser format')
    total = 0
    with open(destination, 'w', encoding='utf-8', newline='') as output:
        for i, piece in enumerate(pieces):
            total += len(piece) + (1 if i else 0)
            if total > maximum:
                raise ValueError('Document exceeds extracted-text limit')
            if i:
                output.write('\n')
            output.write(piece)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Never return document text, filesystem paths, or parser internals to the client.
        sys.exit(2)
