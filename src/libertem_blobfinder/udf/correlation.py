import functools

import numpy as np
import sparseconverter

from libertem.udf import UDF
from libertem.common.container import MaskContainer

from libertem_blobfinder.base import masks
from libertem_blobfinder.common.patterns import MatchPattern
import libertem_blobfinder.base.correlation as ltbc
from libertem_blobfinder.common.correlation import get_peaks


class CorrelationUDF(UDF):
    '''
    Base class for peak correlation implementations
    '''
    def __init__(self, peaks, zero_shift=None, *args, **kwargs):
        '''
        Parameters
        ----------

        peaks : numpy.ndarray
            Numpy array of (y, x) coordinates with peak positions in px to correlate
        zero_shift : Union[AUXBufferWrapper, numpy.ndarray, None], optional
            Zero shift, for example descan error. Can be :code:`None`, :code:`numpy.array((y, x))`
            or AUX data with :code:`(y, x)` for each frame.
        '''
        super().__init__(peaks=np.round(peaks).astype(int), zero_shift=zero_shift, *args, **kwargs)

    def get_result_buffers(self):
        '''
        The common buffers for all correlation methods.

        :code:`centers`:
            (y, x) integer positions. NOTE: the returned positions
            can be out-of-frame and the user should perform bounds
            checking if directly indexing into the frame array.
        :code:`refineds`:
            (y, x) positions with subpixel refinement.
        :code:`peak_values`:
            Peak height in the log scaled frame.
        :code:`peak_elevations`:
            Peak quality (result of :meth:`peak_elevation`).

        See source code for details of the buffer declaration.
        '''
        num_disks = len(self.params.peaks)

        return {
            'centers': self.buffer(
                kind="nav", extra_shape=(num_disks, 2), dtype=np.int32,
            ),
            'refineds': self.buffer(
                kind="nav", extra_shape=(num_disks, 2), dtype="float32"
            ),
            'peak_values': self.buffer(
                kind="nav", extra_shape=(num_disks,), dtype="float32",
            ),
            'peak_elevations': self.buffer(
                kind="nav", extra_shape=(num_disks,), dtype="float32",
            ),
        }

    def output_buffers(self):
        '''
        This function allows abstraction of the result buffers from
        the default implementation in :meth:`get_result_buffers`.

        Override this function if you wish to redirect the results to different
        buffers, for example ragged arrays or binned processing.
        '''
        r = self.results
        return (r.centers, r.refineds, r.peak_values, r.peak_elevations)

    def postprocess(self):
        pass

    def get_peaks(self):
        return self.params.peaks

    def get_zero_shift(self, index=None):
        if self.params.zero_shift is None:
            result = np.array((0, 0))
        elif index is None:
            # Called when masked with view
            result = self.params.zero_shift
        else:
            # Called when not masked, in postprocess() etc.
            result = self.params.zero_shift[index]
        return result


class FastCorrelationUDF(CorrelationUDF):
    '''
    Fourier-based fast correlation-based refinement of peak positions within a search frame
    for each peak.
    '''
    def __init__(self, peaks, match_pattern, zero_shift=None, *args, **kwargs):
        '''
        Parameters
        ----------

        peaks : numpy.ndarray
            Numpy array of (y, x) coordinates with peak positions in px to correlate
        match_pattern : MatchPattern
            Instance of :class:`~libertem_blobfinder.MatchPattern`
        zero_shift : Union[AUXBufferWrapper, numpy.ndarray, None], optional
            Zero shift, for example descan error. Can be :code:`None`, :code:`numpy.array((y, x))`
            or AUX data with :code:`(y, x)` for each frame.
        upsample: Union[bool, int], optional
            Use DFT upsampling for the refinement step, by default False. Supplying
            True will choose a reasonable default upsampling factor, while any
            positive integer > 1 will upsample the correlation peak by this factor.
            DFT upsampling can provide more accurate center values, especially when
            peak shifts are small, but does require more computation time.
        '''
        # For testing purposes, allow to inject a different limit via
        # an internal kwarg
        # It has to come through kwarg because of how UDFs are run
        self.limit = kwargs.get('__limit', 2**19)  # 1/2 MB
        super().__init__(
            peaks=peaks, match_pattern=match_pattern, zero_shift=zero_shift, *args, **kwargs
        )

    def get_task_data(self):
        ""
        n_peaks = len(self.get_peaks())
        mask = self.get_pattern()
        crop_size = mask.get_crop_size()
        template = self.xp.array(mask.get_template(sig_shape=(2 * crop_size, 2 * crop_size)))
        dtype = np.result_type(self.meta.input_dtype, np.float32)
        crop_bufs = ltbc.allocate_crop_bufs(
            crop_size, n_peaks, dtype=dtype, limit=self.limit, xp=self.xp
        )
        if self.meta.array_backend in (
                self.BACKEND_SPARSE_COO, self.BACKEND_SPARSE_GCXS, self.BACKEND_CUPY):
            crop_function = ltbc.crop_disks_from_frame_slicing
        elif self.meta.array_backend in (self.BACKEND_NUMPY, ):
            crop_function = ltbc.crop_disks_from_frame
        else:  # pragma: no cover
            raise RuntimeError(f"Unsupported array backend {self.meta.array_backend}")

        kwargs = {
            'crop_bufs': crop_bufs,
            'template': template,
            'crop_function': crop_function,
        }
        return kwargs

    def get_pattern(self):
        return self.params.match_pattern

    def get_template(self):
        return self.task_data.template

    def process_frame(self, frame):
        match_pattern = self.get_pattern()
        (centers, refineds, peak_values, peak_elevations) = self.output_buffers()
        ltbc.process_frame_fast(
            template=self.get_template(), crop_size=match_pattern.get_crop_size(),
            frame=frame, peaks=self.get_peaks() + np.round(self.get_zero_shift()).astype(int),
            out_centers=centers, out_refineds=refineds,
            out_heights=peak_values, out_elevations=peak_elevations,
            crop_bufs=self.task_data.crop_bufs,
            upsample=self.params.get('upsample', False),
            crop_function=self.task_data.crop_function,
        )

    def get_backends(self):
        return (
            self.BACKEND_NUMPY,
            self.BACKEND_CUPY,
            self.BACKEND_SPARSE_COO,
            self.BACKEND_SPARSE_GCXS,
        )


class FullFrameCorrelationUDF(CorrelationUDF):
    '''
    Fourier-based correlation-based refinement of peak positions within a search
    frame for each peak using a single correlation step. This can be faster for
    correlating a large number of peaks in small frames in comparison to
    :class:`FastCorrelationUDF`. However, it is more sensitive to interference
    from strong peaks next to the peak of interest.

    .. versionadded:: 0.3.0
    '''
    def __init__(self, peaks, match_pattern, zero_shift=None, *args, **kwargs):
        '''
        Parameters
        ----------

        peaks : numpy.ndarray
            Numpy array of (y, x) coordinates with peak positions in px to correlate
        match_pattern : MatchPattern
            Instance of :class:`~libertem_blobfinder.MatchPattern`
        zero_shift : Union[AUXBufferWrapper, numpy.ndarray, None], optional
            Zero shift, for example descan error. Can be :code:`None`, :code:`numpy.array((y, x))`
            or AUX data with :code:`(y, x)` for each frame.
        upsample: Union[bool, int], optional
            Use DFT upsampling for the refinement step, by default False. Supplying
            True will choose a reasonable default upsampling factor, while any
            positive integer > 1 will upsample the correlation peak by this factor.
            DFT upsampling can provide more accurate center values, especially when
            peak shifts are small, but does require more computation time.
        '''
        # For testing purposes, allow to inject a different limit via
        # an internal kwarg
        # It has to come through kwarg because of how UDFs are run
        self.limit = kwargs.get('__limit', 2**19)  # 1/2 MB

        super().__init__(
            peaks=peaks, match_pattern=match_pattern, zero_shift=zero_shift, *args, **kwargs
        )

    def get_task_data(self):
        ""
        mask = self.get_pattern()
        n_peaks = len(self.params.peaks)
        template = self.xp.array(mask.get_template(sig_shape=self.meta.dataset_shape.sig))
        dtype = np.result_type(self.meta.input_dtype, np.float32)
        frame_buf = self.xp.array(
            ltbc.zeros(shape=self.meta.dataset_shape.sig, dtype=dtype)
        )
        crop_size = mask.get_crop_size()

        if self.meta.array_backend in (
                self.BACKEND_SPARSE_COO, self.BACKEND_SPARSE_GCXS, self.BACKEND_CUPY):
            crop_function = ltbc.crop_disks_from_frame_slicing
        elif self.meta.array_backend in (self.BACKEND_NUMPY, ):
            crop_function = ltbc.crop_disks_from_frame
        else:  # pragma: no cover
            raise RuntimeError(f"Unsupported array backend {self.meta.array_backend}")

        kwargs = {
            'template': template,
            'frame_buf': frame_buf,
            'buf_count': ltbc.get_buf_count(crop_size, n_peaks, dtype, self.limit),
            'crop_function': crop_function,
        }
        return kwargs

    def get_pattern(self):
        return self.params.match_pattern

    def get_template(self):
        return self.task_data.template

    def process_frame(self, frame):
        match_pattern = self.get_pattern()
        (centers, refineds, peak_values, peak_elevations) = self.output_buffers()
        ltbc.process_frame_full(
            template=self.get_template(),
            crop_size=match_pattern.get_crop_size(),
            frame=frame,
            peaks=self.get_peaks() + np.round(self.get_zero_shift()).astype(int),
            out_centers=centers,
            out_refineds=refineds,
            out_heights=peak_values,
            out_elevations=peak_elevations,
            frame_buf=self.task_data.frame_buf,
            buf_count=self.task_data.buf_count,
            upsample=self.params.get('upsample', False),
            crop_function=self.task_data.crop_function,
        )

    def get_backends(self):
        # At this time cannot FFT on a full sparse frame so not
        # specifying sparse backends to trigger auto-densification
        return (
            self.BACKEND_NUMPY,
            self.BACKEND_CUPY,
        )


class SparseCorrelationUDF(CorrelationUDF):
    '''
    Direct correlation using sparse matrices

    This method allows to adjust the number of correlation steps independent of the template size.
    '''
    def __init__(self, peaks, match_pattern, steps, *args, **kwargs):
        '''
        Parameters
        ----------

        peaks : numpy.ndarray
            Numpy array of (y, x) coordinates with peak positions in px to correlate
        match_pattern : MatchPattern
            Instance of :class:`~libertem_blobfinder.MatchPattern`
        steps : int
            The template is correlated with 2 * steps + 1 symmetrically around the peak position
            in x and y direction. This defines the maximum shift that can be
            detected. The number of calculations grows with the square of this value, that means
            keeping this as small as the data allows speeds up the calculation.
        '''
        super().__init__(
            peaks=peaks, match_pattern=match_pattern, steps=steps, *args, **kwargs
        )
        if self.params.zero_shift is not None:
            raise ValueError("Parameter zero_shift not supported for SparseCorrelationUDF")

    def get_result_buffers(self):
        """
        This method adds the :code:`corr` buffer to the result of
        :meth:`CorrelationUDF.get_result_buffers`. See source code for the
        exact buffer declaration.
        """
        super_buffers = super().get_result_buffers()
        num_disks = len(self.params.peaks)
        steps = self.params.steps * 2 + 1
        my_buffers = {
            'corr': self.buffer(
                kind="nav", extra_shape=(num_disks * steps**2,), dtype="float32"
            ),
        }
        super_buffers.update(my_buffers)
        return super_buffers

    def get_task_data(self):
        ""
        match_pattern = self.params.match_pattern
        crop_size = match_pattern.get_crop_size()
        size = (2 * crop_size + 1, 2 * crop_size + 1)
        template = match_pattern.get_mask(sig_shape=size)
        steps = self.params.steps
        peak_offsetY, peak_offsetX = np.mgrid[-steps:steps + 1, -steps:steps + 1]

        offsetY = self.params.peaks[:, 0, np.newaxis, np.newaxis] + peak_offsetY - crop_size
        offsetX = self.params.peaks[:, 1, np.newaxis, np.newaxis] + peak_offsetX - crop_size

        offsetY = offsetY.flatten()
        offsetX = offsetX.flatten()

        stack = functools.partial(
            masks.sparse_template_multi_stack,
            mask_index=range(len(offsetY)),
            offsetX=offsetX,
            offsetY=offsetY,
            template=template,
            imageSizeX=self.meta.dataset_shape.sig[1],
            imageSizeY=self.meta.dataset_shape.sig[0]
        )
        if self.meta.array_backend in sparseconverter.CPU_BACKENDS:
            backend = 'numpy'
        elif self.meta.array_backend in sparseconverter.CUDA_BACKENDS:
            backend = 'cupy'
        else:  # pragma: no cover
            raise ValueError("Unknown device class")
        if self.meta.array_backend == self.BACKEND_SPARSE_COO:
            use_sparse = 'sparse.pydata'
        elif self.meta.array_backend == self.BACKEND_SPARSE_GCXS:
            use_sparse = 'sparse.pydata.GCXS'
        elif self.meta.array_backend in (self.BACKEND_CUPY, self.BACKEND_NUMPY):
            use_sparse = 'scipy.sparse.csc'
        else:  # pragma: no cover
            raise RuntimeError(f'Unsupported array backend {self.meta.array_backend}')
        # CSC matrices in combination with transposed data are fastest
        container = MaskContainer(mask_factories=stack, dtype=np.float32,
            use_sparse=use_sparse, backend=backend)

        kwargs = {
            'mask_container': container,
            'crop_size': crop_size,
        }
        return kwargs

    def process_tile(self, tile):
        tile_slice = self.meta.slice
        c = self.task_data.mask_container
        tile_t = ltbc.log_scale(tile.reshape((tile.shape[0], -1)).T, out=None)

        sl = c.get(key=tile_slice, transpose=False)
        self.results.corr[:] += self.forbuf(sl.dot(tile_t).T, self.results.corr)

    def postprocess(self):
        """
        The correlation results are evaluated during postprocessing since this
        implementation uses tiled processing where the correlations are
        incomplete in :meth:`process_tile`.
        """
        steps = 2 * self.params.steps + 1
        corrmaps = self.results.corr.reshape((
            -1,  # frames
            len(self.params.peaks),  # peaks
            steps,  # Y steps
            steps,  # X steps
        ))
        peaks = self.params.peaks
        (centers, refineds, peak_values, peak_elevations) = self.output_buffers()
        for f in range(corrmaps.shape[0]):
            ltbc.evaluate_correlations(
                corrs=corrmaps[f], peaks=peaks, crop_size=self.params.steps,
                out_centers=centers[f], out_refineds=refineds[f],
                out_heights=peak_values[f], out_elevations=peak_elevations[f]
            )

    def get_backends(self):
        return (
            self.BACKEND_NUMPY,
            self.BACKEND_CUPY,
            self.BACKEND_SPARSE_COO,
            self.BACKEND_SPARSE_GCXS
        )


def run_fastcorrelation(
    ctx, dataset, peaks, match_pattern: MatchPattern, zero_shift=None, upsample=False, **kwargs
):
    """
    Wrapper function to construct and run a :class:`FastCorrelationUDF`

    Parameters
    ----------
    ctx : libertem.api.Context
    dataset : libertem.io.dataset.base.DataSet
    peaks : numpy.ndarray
        List of peaks with (y, x) coordinates
    match_pattern : libertem_blobfinder.patterns.MatchPattern
    zero_shift : Union[AUXBufferWrapper, numpy.ndarray, None], optional
        Zero shift, for example descan error. Can be :code:`None`, :code:`numpy.array((y, x))`
        or AUX data with :code:`(y, x)` for each frame.
    upsample : Union[bool, int], optional
        Whether to use upsampling DFT for refinement. False to deactivate (default) or a positive
        integer >1 to upsample by this factor when refining the correlation peak positions. Upsample
        True will choose a sensible upsampling factor.
    kwargs : passed through to :meth:`~libertem.api.Context.run_udf`

    Returns
    -------
    buffers : Dict[libertem.common.buffers.BufferWrapper]
        See :meth:`CorrelationUDF.get_result_buffers` for details.
    """
    peaks = peaks.astype(int)
    udf = FastCorrelationUDF(
        peaks=peaks, match_pattern=match_pattern, zero_shift=zero_shift, upsample=upsample,
    )
    return ctx.run_udf(dataset=dataset, udf=udf, **kwargs)


def run_blobfinder(
    ctx, dataset, match_pattern: MatchPattern, num_peaks, roi=None, upsample=False, progress=False
):
    """
    Wrapper function to find peaks in a dataset and refine their position using
    :class:`FastCorrelationUDF`

    Parameters
    ----------
    ctx : libertem.api.Context
    dataset : libertem.io.dataset.base.DataSet
    match_pattern : libertem_blobfinder.patterns.MatchPattern
    num_peaks : int
        Number of peaks to look for
    roi : numpy.ndarray, optional
        Boolean mask of the navigation dimension to select region of interest (ROI)
    upsample : Union[bool, int], optional
        Whether to use upsampling DFT for refinement. False to deactivate (default) or a positive
        integer >1 to upsample by this factor when refining the correlation peak positions. Upsample
        True will choose a sensible upsampling factor.
    progress : bool, optional
        Show progress bar

    Returns
    -------
    sum_result : numpy.ndarray
        Log-scaled sum frame of the dataset/ROI
    centers, refineds, peak_values, peak_elevations : libertem.common.buffers.BufferWrapper
        See :meth:`CorrelationUDF.get_result_buffers` for details.
    peaks : numpy.ndarray
        List of found peaks with (y, x) coordinates
    """
    if upsample is True:
        upsample = 20

    sum_analysis = ctx.create_sum_analysis(dataset=dataset)
    sum_result = ctx.run(sum_analysis, roi=roi)

    sum_result = ltbc.log_scale(sum_result.intensity.raw_data, out=None)
    peaks = get_peaks(
        sum_result=sum_result,
        match_pattern=match_pattern,
        num_peaks=num_peaks,
    )

    pass_2_results = run_fastcorrelation(
        ctx=ctx,
        dataset=dataset,
        peaks=peaks,
        match_pattern=match_pattern,
        roi=roi,
        upsample=upsample,
        progress=progress
    )

    return (sum_result, pass_2_results['centers'],
        pass_2_results['refineds'], pass_2_results['peak_values'],
        pass_2_results['peak_elevations'], peaks)
